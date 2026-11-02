# Azure 打卡 (astrbot_plugin_azuresignin)

AstrBot 每日打卡插件：打卡、连续天数、积分与排行榜，卡片通过 **WebView(HTML) 渲染**，
文字面板使用 [LiquidGlass](https://github.com/ybouane/liquidglass) 的液态玻璃效果压在底图之上。

## 效果

底图铺满整张卡片，文字面板放在左侧或右侧（可配置），
面板本身是一块会折射底图的玻璃：底图的颜色、亮部会透过面板并发生形变。

## 指令

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/打卡` | `/签到` | 每日打卡，返回玻璃质感卡片 |
| `/打卡排行` | `/签到排行`、`/打卡排行榜` | 查看积分排行榜 |
| `/打卡记录` | `/签到记录`、`/我的打卡` | 查看自己的打卡记录 |

## 配置

全部配置项都在 WebUI 的插件配置页里，分为两组。

### 渲染 (`render`)

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `background_file` | file | 空 | 卡片底图。**这是唯一的背景图配置项**，留空则使用内置渐变背景 |
| `background_url` | string | 空 | 底图网络地址，未上传文件时使用 |
| `text_side` | string | `right` | 文字面板位置：`left` / `right` |
| `card_width` / `card_height` | int | `1000` / `1400` | 卡片尺寸，其余元素按宽度等比缩放 |
| `image_format` | string | `jpeg` | 输出格式：`jpeg` / `png` |
| `scrim_opacity` | float | `0.55` | 底图压暗程度，保证文字可读 |
| `kicker_text` | string | `DAILY CHECK-IN` | 卡片左上的小标题 |
| `footer_text` | string | `愿每一次坚持，都被看见。` | 卡片底部文案 |

### 玻璃 (`render`)

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `glass_enabled` | bool | `true` | 是否启用 WebGL 液态玻璃 |
| `glass_cdn` | string | 空 | LiquidGlass 脚本地址，留空用官方 CDN；国内可换 jsDelivr 镜像 |
| `glass_blur` | float | `0.55` | 模糊程度，调大更朦胧、文字更清晰 |
| `glass_refraction` | float | `0.62` | 折射强度 |
| `glass_tint` | float | `0.42` | 冷色调强度 |
| `glass_brightness` | float | `-0.28` | 亮度，负值压暗面板 |
| `glass_saturation` | float | `-0.30` | 饱和度 |
| `glass_radius` | int | `52` | 面板圆角 |
| `glass_shadow` | float | `0.34` | 面板投影强度 |

### 打卡规则 (`signin`)

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `points_per_sign_in` | int | `10` | 每次打卡的基础积分 |
| `streak_bonus` | int | `2` | 每多连续一天额外增加的积分 |
| `max_streak_bonus` | int | `5` | 连续加成最多叠加的天数 |
| `reset_hour` | int | `0` | 每日几点重置（跨天打卡的判定点） |
| `leaderboard_size` | int | `10` | 排行榜显示人数 |

单次积分 = `points_per_sign_in + streak_bonus × min(连续天数 - 1, max_streak_bonus)`。

## 字体

卡片统一使用 **Noto Sans SC**（思源黑体），从 jsDelivr 的 Fontsource 分片加载 400/500/700/900 四档字重。

## 渲染说明

卡片走 AstrBot 的 `html_render`，也就是在渲染服务的 Chromium 里执行真实的 HTML/CSS/JS：

1. 底图由 Python 侧缩放压缩成 data URI 后注入，避免跨域导致 WebGL 画布被污染。
2. LiquidGlass 通过 CDN 动态 `import`，`init()` 之后等 shader 稳定几帧再交接玻璃层。
3. **失败即降级**：没有 WebGL、CDN 不可达、或使用纯渐变背景时，面板自动退回
   一层 CSS 毛玻璃（`backdrop-filter` + 底图模糊），卡片照常出图。
4. 渲染彻底失败时，指令会退回纯文本播报，不会静默失败。

由于液态玻璃需要在渲染端执行 JS，请使用支持 HTML + JS 的渲染方式
（默认的远程 `t2i` 渲染服务或本地 Playwright 渲染均可）。

## 数据

打卡数据通过 AstrBot 的 KV 存储保存（`user_<uid>` 与 `board`），
卡片缓存位于 `data/plugin_data/astrbot_plugin_azuresignin/cache/`，插件启动时清理 24 小时前的旧图。

## 依赖

`requirements.txt`：`aiohttp`、`pillow`。
