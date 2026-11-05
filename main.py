"""Azure 打卡：每日打卡 + WebView(HTML) 渲染的玻璃质感卡片插件。

卡片由 ``templates/signin_card.html`` 渲染（HTML + CSS + Jinja2），
底图与玻璃面板的层次参考了 https://github.com/ybouane/liquidglass
的用法：底图为同级元素，文字面板作为玻璃元素叠加在其上，
WebGL 不可用时自动退化为 CSS 毛玻璃。
"""

from __future__ import annotations

import asyncio
import asyncio
import json
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import jinja2

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

class HtmlRenderer:
    """Render plugin HTML to an image with a headless Chromium via Playwright.

    AstrBot's built-in t2i either calls a remote service or leans on Pillow, and
    neither fits a plugin whose artwork lives on the local disk. Driving
    Chromium in-process keeps rendering local and lets templates point straight
    at ``file://`` assets.
    """

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self._playwright = None
        self._browser = None
        self._context = None
        # 允许 2 个页面同时渲染；浏览器复用同一个 context 以保留 HTTP 缓存。
        self._lock = asyncio.Semaphore(2)

    async def _ensure_browser(self):
        """Start Chromium on first use and reuse it afterwards.

        Returns:
            The connected browser instance.

        Raises:
            RuntimeError: When playwright or its browser is not installed.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "缺少 playwright，请先执行：pip install playwright && playwright install chromium"
            ) from exc
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                # 允许画布回读 file:// 图片的像素，模板用它判断玻璃厚度。
                args=[
                    "--no-sandbox",
                    "--allow-file-access-from-files",
                    "--font-render-hinting=none",
                ],
            )
        except Exception as exc:
            await self._playwright.stop()
            self._playwright = None
            raise RuntimeError("Chromium 未就绪，请执行：playwright install chromium") from exc
        return self._browser

    async def render(
        self,
        html: str,
        out_path: Path,
        image_format: str = "jpeg",
        quality: int = 92,
    ) -> Path | None:
        """把 HTML 渲染成图片。

        Args:
            html: 完整文档。
            out_path: 输出路径。
            image_format: ``jpeg`` 或 ``png``。
            quality: JPEG 质量。

        Returns:
            写出成功时返回 ``out_path``，否则 None。
        """
        source = self.work_dir / f"page_{uuid.uuid4().hex}.html"
        try:
            source.write_text(html, encoding="utf-8")
        except OSError as exc:
            logger.error(f"写入渲染页面失败：{exc}")
            return None

        async with self._lock:
            try:
                browser = await self._ensure_browser()
                if self._context is None:
                    self._context = await browser.new_context(
                        viewport={"width": 2200, "height": 1600},
                        device_scale_factor=1,
                    )
                context = self._context
            except RuntimeError:
                source.unlink(missing_ok=True)
                raise
            page = await context.new_page()
            try:
                await page.goto(source.as_uri(), wait_until="load")
                try:
                    await page.wait_for_function(
                        "window.__renderReady === true", timeout=12000
                    )
                except Exception:
                    logger.warning("模板未上报 __renderReady，按当前状态截图")
                try:
                    await page.evaluate("document.fonts && document.fonts.ready")
                except Exception:
                    pass
                await page.wait_for_timeout(80)

                size = await page.evaluate(
                    "() => { const r = document.getElementById('root').getBoundingClientRect();"
                    " return {w: Math.ceil(r.width), h: Math.ceil(r.height)}; }"
                )
                width, height = int(size["w"]), int(size["h"])
                if width <= 0 or height <= 0:
                    logger.error("渲染页面尺寸异常")
                    return None

                await page.set_viewport_size({"width": width, "height": height})
                await page.wait_for_timeout(60)
                shot = {"path": str(out_path), "clip": {
                    "x": 0, "y": 0, "width": width, "height": height}}
                if image_format == "png":
                    await page.screenshot(type="png", **shot)
                else:
                    await page.screenshot(type="jpeg", quality=quality, **shot)
            except Exception:
                logger.error("Playwright 渲染失败", exc_info=True)
                return None
            finally:
                await page.close()
                source.unlink(missing_ok=True)
        return out_path if out_path.is_file() else None

    async def close(self) -> None:
        """释放浏览器与 playwright 驱动。"""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


PLUGIN_NAME = "astrbot_plugin_azuresignin"

#: 同一条消息可能同时命中 command 和 regex，用这个标记保证只打卡一次。
HANDLED_KEY = "azuresignin_handled"
TEMPLATE_PATH = Path(__file__).parent / "templates" / "signin_card.html"
SIGNATURE_PATH = Path(__file__).parent / "assets" / "signature.png"
DEFAULT_BACKGROUND_PATH = Path(__file__).parent / "assets" / "default_bg.jpg"

#: LiquidGlass 的 ESM 入口。插件不做本地托管：卡片是在渲染服务（WebView）里执行的。
LIQUID_GLASS_CDN = "https://cdn.jsdmirror.com/npm/@ybouane/liquidglass/dist/index.js"
#: 渲染配置默认值（与 _conf_schema.json 保持一致，用户配置缺失时兜底）。
RENDER_DEFAULTS = {
    "background_file": [],
    "background_url": "",
    "background_position": "",
    "text_side": "right",
    "card_width": 1000,
    "card_height": 1400,
    "image_format": "jpeg",
    "scrim_opacity": 0.55,
    "glass_enabled": True,
    "glass_cdn": "",
    "glass_blur": 0.72,
    "glass_refraction": 0.42,
    "glass_tint": 0.18,
    "glass_brightness": 0.16,
    "glass_saturation": -0.10,
    "glass_radius": 46,
    "glass_shadow": 0.20,
    "signature_file": [],
    "kicker_text": "DAILY CHECK-IN",
    "footer_text": "愿每一次坚持，都被看见。",
}

#: 打卡规则默认值。
SIGNIN_DEFAULTS = {
    "points_per_sign_in": 10,
    "streak_bonus": 2,
    "max_streak_bonus": 5,
    "reset_hour": 0,
    "leaderboard_size": 10,
}

WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class AzureSignInPlugin(Star):
    """每日打卡插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        render_cfg = self.config.get("render")
        self.render_cfg = dict(RENDER_DEFAULTS)
        if isinstance(render_cfg, dict):
            self.render_cfg.update(render_cfg)

        signin_cfg = self.config.get("signin")
        self.signin_cfg = dict(SIGNIN_DEFAULTS)
        if isinstance(signin_cfg, dict):
            self.signin_cfg.update(signin_cfg)

        try:
            self.template = TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            self.template = ""
            logger.error(f"读取打卡卡片模板失败：{exc}")

        # 允许 2 个页面同时渲染；浏览器复用同一个 context 以保留 HTTP 缓存。
        self._lock = asyncio.Semaphore(2)
        self._sign_cache: str | None = None
        self._env = jinja2.Environment(autoescape=False)
        self.renderer = HtmlRenderer(self.cache_dir)

    async def terminate(self) -> None:
        """插件卸载时关掉无头浏览器。"""
        await self.renderer.close()

    async def initialize(self) -> None:
        """插件激活时清理过期的卡片缓存。"""
        deadline = time.time() - 24 * 3600
        for entry in self.cache_dir.glob("card_*"):
            try:
                if entry.stat().st_mtime < deadline:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue

    # ------------------------------------------------------------------ 配置

    def _int(self, cfg: dict, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(cfg.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _float(self, cfg: dict, key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(cfg.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _bool(self, cfg: dict, key: str, default: bool) -> bool:
        value = cfg.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(default if value is None else value)

    # ------------------------------------------------------------------ 数据

    def _sign_day(self) -> date:
        """返回当前“签到日”。

        配置了 ``reset_hour`` 时，凌晨未过该小时仍算作前一天，
        方便熬夜的用户。

        Returns:
            用于判定打卡日期的 date。
        """
        reset_hour = self._int(self.signin_cfg, "reset_hour", 0, 0, 23)
        return (datetime.now() - timedelta(hours=reset_hour)).date()

    @staticmethod
    def _uid(event: AstrMessageEvent) -> str:
        """取发送者 ID。

        ``AstrMessageEvent.get_sender_id()`` 在 ``sender.user_id`` 不是字符串
        时会直接返回空串（见 astr_message_event.py 的实现），那样所有用户会
        落进同一个空 key，积分和记录就会被"共用"。这里再兜一层原始字段。

        Args:
            event: 消息事件。

        Returns:
            发送者 ID；确实取不到时返回空字符串。
        """
        uid = event.get_sender_id()
        if uid:
            return str(uid)
        sender = getattr(event.message_obj, "sender", None)
        for attr in ("user_id", "id", "uin"):
            value = getattr(sender, attr, None)
            if value not in (None, ""):
                return str(value)
        return ""

    async def _load_record(self, uid: str) -> dict:
        """读取并规整某个用户的打卡记录。

        Args:
            uid: 用户 ID。

        Returns:
            包含 name/last_day/streak/best/total_days/points 的记录字典；
            缺失或类型不符的字段回落到默认值。
        """
        data = await self.get_kv_data(f"user_{uid}", {})
        record = {
            "name": "",
            "last_day": "",
            "streak": 0,
            "best": 0,
            "total_days": 0,
            "points": 0,
        }
        if not isinstance(data, dict):
            return record
        for key in ("name", "last_day"):
            if isinstance(data.get(key), str):
                record[key] = data[key]
        for key in ("streak", "best", "total_days", "points"):
            if isinstance(data.get(key), int):
                record[key] = data[key]
        return record

    async def _load_board(self) -> dict:
        board = await self.get_kv_data("board", {})
        return board if isinstance(board, dict) else {}

    async def _save_board(self, board: dict) -> None:
        # 只保留可用于排行的字段，避免排行榜体积随记录无限膨胀。
        compact = {
            uid: {
                "name": str(row.get("name", ""))[:64],
                "points": int(row.get("points", 0) or 0),
                "total_days": int(row.get("total_days", 0) or 0),
            }
            for uid, row in board.items()
            if isinstance(row, dict)
        }
        await self.put_kv_data("board", compact)

    @staticmethod
    def _board_sorted(board: dict) -> list[tuple[str, dict]]:
        return sorted(
            board.items(),
            key=lambda kv: (
                -int(kv[1].get("points", 0) or 0),
                -int(kv[1].get("total_days", 0) or 0),
                str(kv[0]),
            ),
        )

    # ------------------------------------------------------------- 背景与渲染

    def _side(self) -> str:
        """返回文字面板所在的一侧，只可能是 ``left`` 或 ``right``。"""
        return (
            "left"
            if str(self.render_cfg.get("text_side", "right")).lower() == "left"
            else "right"
        )

    def _to_url(self, src: str) -> str:
        """把配置里的图片引用统一成页面可直接加载的 URL。

        上传文件存的是相对插件数据目录的路径；也接受 http(s) 直链、file://
        与本机绝对路径。

        Args:
            src: 配置值。

        Returns:
            可用的 URL；图片不存在时返回空字符串。
        """
        if src.startswith(("http://", "https://")):
            return src
        path = Path(src[7:]) if src.startswith("file://") else Path(src)
        if not path.is_absolute():
            path = self.data_dir / path
        return path.as_uri() if path.is_file() else ""

    async def _resolve_background(self) -> dict:
        """解析卡片底图。

        Returns:
            ``{"is_image": bool, "src": str, "css": str, "is_dark": bool}``。
            明暗由页面内的画布采样决定，这里不再计算。
        """
        gradient = {"is_image": False, "src": "", "css": "var(--sky)", "is_dark": False}
        uploaded = self.render_cfg.get("background_file")
        if isinstance(uploaded, str):
            candidates = [uploaded]
        elif isinstance(uploaded, list):
            candidates = [item for item in uploaded if isinstance(item, str)]
        else:
            candidates = []

        fallback_url = str(self.render_cfg.get("background_url") or "").strip()
        if fallback_url:
            candidates.append(fallback_url)

        # 都没配就走内置底图，让插件装好即有天依的观感。
        if not any(src.strip() for src in candidates):
            candidates.append(str(DEFAULT_BACKGROUND_PATH))

        for src in candidates:
            url = self._to_url(src.strip())
            if url:
                return {
                    "is_image": True,
                    "src": url,
                    "css": f'url("{url}")',
                    "is_dark": False,
                }
        return gradient

    async def _resolve_sign(self) -> str:
        """解析落款线稿：优先用配置里上传的图，否则用插件内置的手绘线稿。

        Returns:
            可直接放进 ``<img src>`` 的 URL；不可用时返回空字符串。
        """
        if self._sign_cache is not None:
            return self._sign_cache

        uploaded = self.render_cfg.get("signature_file")
        if isinstance(uploaded, str):
            candidates = [uploaded]
        elif isinstance(uploaded, list):
            candidates = [item for item in uploaded if isinstance(item, str)]
        else:
            candidates = []

        for src in candidates:
            url = self._to_url(src.strip())
            if url:
                self._sign_cache = url
                return url

        self._sign_cache = SIGNATURE_PATH.as_uri() if SIGNATURE_PATH.is_file() else ""
        return self._sign_cache

    def _glass_json(self, has_image: bool) -> str:
        """生成注入到卡片脚本里的 LiquidGlass 配置。

        Args:
            has_image: 本次是否使用真实背景图。LiquidGlass 只能采样同级的 ``<img>``，
                纯渐变背景会渲染成一块发白的面板，因此这种情况交给 CSS 毛玻璃。

        默认值偏"磨砂"：模糊高、略微提亮、轻降饱和。玻璃渲染的是折射后的底图，
        本身是透明的，真正托住文字的是模板里的白雾层；这里只负责玻璃的质感。
        """
        options = {
            "blurAmount": self._float(self.render_cfg, "glass_blur", 0.72, 0.0, 1.0),
            "refraction": self._float(self.render_cfg, "glass_refraction", 0.42, 0.0, 1.5),
            "chromAberration": 0.05,
            "edgeHighlight": 0.09,
            "specular": 0.40,
            "fresnel": 0.95,
            "distortion": 0.02,
            "cornerRadius": self._int(self.render_cfg, "glass_radius", 46, 0, 200),
            "zRadius": 36,
            "opacity": 1.0,
            "saturation": self._float(self.render_cfg, "glass_saturation", -0.10, -1.0, 1.0),
            "tintStrength": self._float(self.render_cfg, "glass_tint", 0.18, 0.0, 1.0),
            "brightness": self._float(self.render_cfg, "glass_brightness", 0.16, -0.6, 0.4),
            "shadowOpacity": self._float(self.render_cfg, "glass_shadow", 0.20, 0.0, 1.0),
            "shadowSpread": 18,
            "shadowOffsetY": 6,
            "floating": False,
            "button": False,
            "bevelMode": 0,
        }
        cdn = str(self.render_cfg.get("glass_cdn") or "").strip() or LIQUID_GLASS_CDN
        payload = {
            "enabled": self._bool(self.render_cfg, "glass_enabled", True),
            "hasImage": bool(has_image),
            "cdn": cdn,
            "options": options,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _render_card(self, event: AstrMessageEvent, text: dict) -> str | None:
        """渲染打卡卡片并裁切到配置尺寸。

        Args:
            event: 触发渲染的消息事件。
            text: 卡片上的文本数据。

        Returns:
            本地图片路径；渲染失败时返回 None。
        """
        if not self.template:
            return None

        width = self._int(self.render_cfg, "card_width", 1000, 480, 2400)
        height = self._int(self.render_cfg, "card_height", 1400, 640, 3200)
        side = self._side()
        image_format = str(self.render_cfg.get("image_format", "jpeg")).lower()
        if image_format not in {"jpeg", "png"}:
            image_format = "jpeg"

        background = await self._resolve_background()
        # 横版插画塞进竖版卡片时会被 cover 裁掉两侧，把主体让到没有文字的一侧。
        bg_position = self.render_cfg.get("background_position")
        if not isinstance(bg_position, str) or not bg_position.strip():
            bg_position = "72% center" if side == "right" else "28% center"

        data = {
            "card_w": width,
            "card_h": height,
            "scale": round(width / 1000, 4),
            "side": side,
            "bg": background,
            "bg_position": bg_position.strip(),
            "is_dark": background["is_dark"],
            "scrim_opacity": self._float(self.render_cfg, "scrim_opacity", 0.55, 0.0, 1.0),
            "glass_json": self._glass_json(background["is_image"]),
            "sign_src": await self._resolve_sign(),
            "text": text,
        }

        try:
            html = self._env.from_string(self.template).render(**data)
        except Exception:
            self.logger.error("打卡模板渲染失败。", exc_info=True)
            return None

        out_path = self.cache_dir / f"card_{uuid.uuid4().hex}.{image_format}"
        try:
            rendered = await self.renderer.render(html, out_path, image_format=image_format)
        except RuntimeError as exc:
            self.logger.error(f"渲染器不可用：{exc}")
            return None
        return str(rendered) if rendered else None

    # ------------------------------------------------------------------ 业务

    def _points_for(self, streak: int) -> int:
        """计算某次连续天数对应的积分。"""
        base = self._int(self.signin_cfg, "points_per_sign_in", 10, 0, 100000)
        bonus = self._int(self.signin_cfg, "streak_bonus", 2, 0, 100000)
        cap = self._int(self.signin_cfg, "max_streak_bonus", 5, 0, 365)
        return base + bonus * min(max(streak - 1, 0), cap)

    async def _sign_in(self, uid: str, name: str) -> tuple[dict, bool, int]:
        """执行一次打卡并落库。

        Args:
            uid: 用户 ID。
            name: 用户昵称。

        Returns:
            (打卡后的记录, 本次是否为新的打卡, 本次获得的积分)
        """
        async with self._lock:
            today = self._sign_day()
            today_str = today.isoformat()
            record = await self._load_record(uid)
            record["name"] = name

            if record["last_day"] == today_str:
                return record, False, 0

            yesterday = (today - timedelta(days=1)).isoformat()
            record["streak"] = record["streak"] + 1 if record["last_day"] == yesterday else 1
            record["last_day"] = today_str
            record["total_days"] += 1
            gained = self._points_for(record["streak"])
            record["points"] += gained
            record["best"] = max(record["best"], record["streak"])

            await self.put_kv_data(f"user_{uid}", record)

            board = await self._load_board()
            board[uid] = {
                "name": name,
                "points": record["points"],
                "total_days": record["total_days"],
            }
            await self._save_board(board)
            return record, True, gained

    async def _rank_of(self, uid: str) -> tuple[int, int]:
        """返回该用户的名次与参与人数。

        Args:
            uid: 用户 ID。

        Returns:
            (名次, 总人数)；未上榜时名次为总人数。
        """
        board = await self._load_board()
        rows = self._board_sorted(board)
        for index, (board_uid, _row) in enumerate(rows, start=1):
            if board_uid == uid:
                return index, len(rows)
        return len(rows), len(rows)

    async def _build_text(self, event: AstrMessageEvent, record: dict, signed: bool, gained: int) -> dict:
        """组装卡片上的文案。"""
        today = self._sign_day()
        rank, total_users = await self._rank_of(self._uid(event))
        return {
            "kicker": str(self.render_cfg.get("kicker_text") or "DAILY CHECK-IN"),
            "date": f"{today.year}.{today.month:02d}.{today.day:02d}",
            "weekday": WEEKDAY_NAMES[today.weekday()],
            "name": record.get("name") or event.get_sender_name(),
            "streak": record["streak"],
            "total_days": record["total_days"],
            "points": record["points"],
            "rank": f"#{rank}" if total_users else "#-",
            "status_title": "打卡成功" if signed else "今日已打卡",
            "status_points": f"+{gained} 积分" if signed else "明天再来",
            "footer": str(self.render_cfg.get("footer_text") or ""),
        }

    @staticmethod
    def _fallback_text(record: dict, signed: bool, gained: int) -> str:
        """渲染失败时的纯文本兜底。"""
        if signed:
            return (
                f"打卡成功！连续 {record['streak']} 天，"
                f"今日 +{gained} 积分（累计 {record['points']} 分 / {record['total_days']} 天）"
            )
        return f"今天已经打过卡啦，连续 {record['streak']} 天，累计 {record['points']} 分。"

    # ---------------------------------------------------------------- 指令

    @filter.command("打卡", alias={"签到"})
    async def sign_in(self, event: AstrMessageEvent):
        """每日打卡，生成玻璃质感打卡卡片"""
        # ``filter.command`` 与 ``filter.regex`` 可能同时命中同一条消息
        # （私聊会自动唤醒），这里用事件级标记保证只处理一次。
        if event.get_extra(HANDLED_KEY):
            return
        event.set_extra(HANDLED_KEY, True)

        uid = self._uid(event)
        if not uid:
            self.logger.error("无法获取发送者 ID，可能是适配器未提供 sender。")
            yield event.plain_result("取不到你的账号 ID，无法记录打卡。请把这条报错发给管理员。")
            return
        name = event.get_sender_name()
        try:
            record, signed, gained = await self._sign_in(uid, name)
        except Exception:
            self.logger.error("打卡数据写入失败。", exc_info=True)
            yield event.plain_result("打卡失败，请稍后再试。")
            return

        try:
            text = await self._build_text(event, record, signed, gained)
            path = await self._render_card(event, text)
        except Exception:
            self.logger.error("打卡卡片生成失败。", exc_info=True)
            path = None

        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result(self._fallback_text(record, signed, gained))

    @filter.regex(r"^(打卡|签到)[!！。.]*$")
    async def sign_in_without_prefix(self, event: AstrMessageEvent):
        """群聊里直接说「签到」「打卡」也能打卡，不需要前缀或 @ 机器人。

        AstrBot 的 ``filter.command`` 要求消息先被唤醒（前缀或 @），
        而 ``filter.regex`` 不受此限制，所以用它兜住"裸"签到。
        带前缀 / @ / 私聊时两个过滤器会同时命中，去重交给 ``sign_in``
        里的事件标记，这里的实现保持和指令入口完全一致。
        """
        async for result in self.sign_in(event):
            yield result

    @filter.command("打卡排行", alias={"签到排行", "打卡排行榜"})
    async def leaderboard(self, event: AstrMessageEvent):
        """查看打卡积分排行榜"""
        board = await self._load_board()
        rows = self._board_sorted(board)
        if not rows:
            yield event.plain_result("还没有人打卡，快去发一条打卡指令抢第一吧！")
            return

        size = self._int(self.signin_cfg, "leaderboard_size", 10, 1, 50)
        lines = [f"🏆 打卡排行榜（共 {len(rows)} 人）"]
        for index, (_uid, row) in enumerate(rows[:size], start=1):
            name = str(row.get("name") or "匿名")
            lines.append(
                f"{index}. {name} —— {int(row.get('points', 0) or 0)} 分 · "
                f"{int(row.get('total_days', 0) or 0)} 天"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("打卡记录", alias={"签到记录", "我的打卡"})
    async def my_record(self, event: AstrMessageEvent):
        """查看自己的打卡记录"""
        record = await self._load_record(self._uid(event))
        if record["total_days"] <= 0:
            yield event.plain_result("你还没有打卡记录，发一条打卡指令开始第一天吧！")
            return

        rank, total_users = await self._rank_of(self._uid(event))
        yield event.plain_result(
            f"📅 累计签到：{record['total_days']} 天\n"
            f"🔥 当前连续：{record['streak']} 天（最长 {record['best']} 天）\n"
            f"💎 总积分：{record['points']}\n"
            f"🏅 排名：第 {rank} / {total_users} 名"
        )
