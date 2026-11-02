"""Azure 打卡：每日打卡 + WebView(HTML) 渲染的玻璃质感卡片插件。

卡片由 ``templates/signin_card.html`` 渲染（HTML + CSS + Jinja2），
底图与玻璃面板的层次参考了 https://github.com/ybouane/liquidglass
的用法：底图为同级元素，文字面板作为玻璃元素叠加在其上，
WebGL 不可用时自动退化为 CSS 毛玻璃。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
from PIL import Image as PILImage

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

PLUGIN_NAME = "astrbot_plugin_azuresignin"
TEMPLATE_PATH = Path(__file__).parent / "templates" / "signin_card.html"

#: LiquidGlass 的 ESM 入口。插件不做本地托管：卡片是在渲染服务（WebView）里执行的。
LIQUID_GLASS_CDN = "https://cdn.jsdelivr.net/npm/@ybouane/liquidglass@1/dist/index.js"
#: 渲染配置默认值（与 _conf_schema.json 保持一致，用户配置缺失时兜底）。
RENDER_DEFAULTS = {
    "background_file": [],
    "background_url": "",
    "text_side": "right",
    "card_width": 1000,
    "card_height": 1400,
    "image_format": "jpeg",
    "scrim_opacity": 0.55,
    "glass_enabled": True,
    "glass_cdn": "",
    "glass_blur": 0.55,
    "glass_refraction": 0.62,
    "glass_tint": 0.42,
    "glass_brightness": -0.28,
    "glass_saturation": -0.30,
    "glass_radius": 52,
    "glass_shadow": 0.34,
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

        self._lock = asyncio.Lock()
        self._bg_cache: dict[str, str] = {}

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

    async def _to_data_uri(self, src: str) -> str | None:
        """把背景图转成 data URI，避免跨域图片污染 canvas 导致玻璃效果失效。

        Args:
            src: http(s) 链接、file:// URI 或本地路径。

        Returns:
            可直接放进 ``<img src>`` 的 data URI；失败时返回 None。
        """
        if src.startswith("data:"):
            return src

        raw: bytes | None = None
        cache_key = src
        if src.startswith(("http://", "https://")):
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    async with session.get(src) as resp:
                        if resp.status != 200:
                            self.logger.warning(f"背景图下载失败（HTTP {resp.status}）：{src}")
                            return None
                        raw = await resp.read()
            except Exception as exc:
                self.logger.warning(f"背景图下载异常：{exc}")
                return None
        else:
            path = Path(src[7:]) if src.startswith("file://") else Path(src)
            if not path.is_absolute():
                path = self.data_dir / path
            if not path.is_file():
                self.logger.warning(f"背景图不存在：{path}")
                return None
            try:
                raw = path.read_bytes()
                cache_key = f"{path}|{path.stat().st_mtime_ns}"
            except OSError as exc:
                self.logger.warning(f"读取背景图失败：{exc}")
                return None

        cached = self._bg_cache.get(cache_key)
        if cached:
            return cached

        data_uri = await asyncio.to_thread(self._encode_image, raw)
        if data_uri:
            if len(self._bg_cache) >= 8:
                self._bg_cache.clear()
            self._bg_cache[cache_key] = data_uri
        return data_uri

    @staticmethod
    def _encode_image(raw: bytes) -> str | None:
        """压缩并编码背景图，控制发给渲染服务的数据量。

        Args:
            raw: 原始图片字节。

        Returns:
            JPEG 的 data URI；无法解码时返回 None。
        """
        try:
            with PILImage.open(io.BytesIO(raw)) as img:
                img = img.convert("RGB")
                img.thumbnail((1600, 2400), PILImage.Resampling.LANCZOS)
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=88, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                return f"data:image/jpeg;base64,{encoded}"
        except Exception:
            return None

    async def _resolve_background(self) -> dict:
        """解析底部背景图，未配置时退回内置渐变背景。

        Returns:
            ``{"is_image": bool, "src": str, "css": str}``。
            ``css`` 供模板里 CSS 毛玻璃兜底层复用同一张底图。
        """
        gradient = {"is_image": False, "src": "", "css": "var(--aurora)"}
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

        for src in candidates:
            src = src.strip()
            if not src:
                continue
            data_uri = await self._to_data_uri(src)
            if data_uri:
                return {"is_image": True, "src": data_uri, "css": f'url("{data_uri}")'}
            if src.startswith(("http://", "https://")):
                # 下载失败时至少把原始链接交出去，渲染端若可直连仍能显示。
                self.logger.warning(f"背景图预下载失败，改用原始链接：{src}")
                return {"is_image": True, "src": src, "css": f'url("{src}")'}
        return gradient

    def _glass_json(self, has_image: bool) -> str:
        """生成注入到卡片脚本里的 LiquidGlass 配置。

        Args:
            has_image: 本次是否使用真实背景图。LiquidGlass 只能采样同级的 ``<img>``，
                纯渐变背景会渲染成一块发白的面板，因此这种情况交给 CSS 毛玻璃。

        默认值是"能压住花哨底图"的一档：模糊偏高、整体压暗、略降饱和，
        保证白字在照片上依然清晰；想要更通透可以自行调小。
        """
        options = {
            "blurAmount": self._float(self.render_cfg, "glass_blur", 0.55, 0.0, 1.0),
            "refraction": self._float(self.render_cfg, "glass_refraction", 0.62, 0.0, 1.5),
            "chromAberration": 0.05,
            "edgeHighlight": 0.09,
            "specular": 0.40,
            "fresnel": 0.95,
            "distortion": 0.02,
            "cornerRadius": self._int(self.render_cfg, "glass_radius", 52, 0, 200),
            "zRadius": 36,
            "opacity": 1.0,
            "saturation": self._float(self.render_cfg, "glass_saturation", -0.30, -1.0, 1.0),
            "tintStrength": self._float(self.render_cfg, "glass_tint", 0.42, 0.0, 1.0),
            "brightness": self._float(self.render_cfg, "glass_brightness", -0.28, -0.6, 0.4),
            "shadowOpacity": self._float(self.render_cfg, "glass_shadow", 0.34, 0.0, 1.0),
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
        side = "left" if str(self.render_cfg.get("text_side", "right")).lower() == "left" else "right"
        image_format = str(self.render_cfg.get("image_format", "jpeg")).lower()
        if image_format not in {"jpeg", "png"}:
            image_format = "jpeg"

        background = await self._resolve_background()
        data = {
            "card_w": width,
            "card_h": height,
            "scale": round(width / 1000, 4),
            "side": side,
            "bg": background,
            "scrim_opacity": self._float(self.render_cfg, "scrim_opacity", 0.55, 0.0, 1.0),
            "glass_json": self._glass_json(background["is_image"]),
            "text": text,
        }

        options = {
            "full_page": True,
            "type": image_format,
            "animations": "disabled",
            "caret": "hide",
        }
        if image_format == "jpeg":
            options["quality"] = 92

        try:
            raw_path = await self.html_render(
                self.template,
                data,
                return_url=False,
                options=options,
                umo=event.unified_msg_origin,
            )
        except Exception:
            self.logger.error("打卡卡片渲染失败。", exc_info=True)
            return None

        if not raw_path:
            return None

        out_path = self.cache_dir / f"card_{uuid.uuid4().hex}.{image_format}"
        try:
            with PILImage.open(raw_path) as img:
                if img.width < width or img.height < height:
                    self.logger.warning(
                        f"渲染结果小于卡片尺寸（{img.width}x{img.height} < {width}x{height}），跳过裁切。"
                    )
                else:
                    img = img.crop((0, 0, width, height))
                if image_format == "jpeg":
                    img.convert("RGB").save(out_path, format="JPEG", quality=92)
                else:
                    img.save(out_path, format="PNG")
            return str(out_path)
        except Exception:
            self.logger.warning("卡片后处理失败，直接使用渲染原图。", exc_info=True)
            return raw_path

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
        rank, total_users = await self._rank_of(event.get_sender_id())
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
        uid = event.get_sender_id()
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
        带前缀 / @ / 私聊时两个过滤器会同时命中，这里交还给指令处理器，
        避免同一条消息触发两次打卡。
        """
        if event.is_at_or_wake_command:
            return

        async for result in self.sign_in(event):
            yield result

    @filter.command("打卡排行", alias={"签到排行", "打卡排行榜"})
    async def leaderboard(self, event: AstrMessageEvent):
        """查看打卡积分排行榜"""
        board = await self._load_board()
        rows = self._board_sorted(board)
        if not rows:
            yield event.plain_result("还没有人打卡，快去发送 /打卡 抢第一吧！")
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
        record = await self._load_record(event.get_sender_id())
        if record["total_days"] <= 0:
            yield event.plain_result("你还没有打卡记录，发送 /打卡 开始第一天吧！")
            return

        rank, total_users = await self._rank_of(event.get_sender_id())
        yield event.plain_result(
            f"📅 累计签到：{record['total_days']} 天\n"
            f"🔥 当前连续：{record['streak']} 天（最长 {record['best']} 天）\n"
            f"💎 总积分：{record['points']}\n"
            f"🏅 排名：第 {rank} / {total_users} 名"
        )
