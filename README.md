# MaiBot Bilibili Dynamic

由 [EzraRT](https://github.com/EzraRT) 维护的 Python / MaiBot 插件：订阅 UP 主、关注流低延迟检测、动态和直播推送、番剧更新、聊天链接卡片。

项目仓库：[EzraRT/MaiBot-Bilibili-Dynamic](https://github.com/EzraRT/MaiBot-Bilibili-Dynamic)。问题反馈请提交到 [Issues](https://github.com/EzraRT/MaiBot-Bilibili-Dynamic/issues)。

适配当前 [MaiBot 官方开发教程](https://docs.mai-mai.org/plugin/)，使用 `MaiBotPlugin`、`@Command`、`@EventHandler` 和 Host 能力代理。要求 **Python 3.12+、MaiBot 1.3.5+ 的新插件运行时、maibot-plugin-sdk 2.11.x**。旧版 `BasePlugin` 接口不适用。

## 功能

| 功能 | 实现 |
| --- | --- |
| 订阅管理 | 按当前聊天流隔离；添加、删除、列表、暂停、恢复；默认仅管理员可修改 |
| 动态检测 | 登录后的关注动态流默认每 10 秒检查；分页补查；主页轮询兜底 |
| 动态推送 | 文字、图文、视频、专栏、音乐和转发动态；原动态已删除时仍可显示转发正文 |
| 直播 | 批量查询 UP 主状态，推送开播和下播；首次观察建立基线，避免推送历史开播 |
| 番剧 | 接受 `ss`、`ep`、`md` 号，统一订阅为 `ss`；新可播放集数提醒 |
| 链接卡片 | 视频、动态、opus、专栏、直播间、UP 主页、番剧、b23.tv / bili2233.cn 短链 |
| 分享消息 | 读取聊天文本和 JSON/XML 分享消息；解析独立 BV、av、cv、ss、ep、md 号 |
| 过滤 | 每个订阅独立设置类型或正则黑白名单；可匹配转发原文 |
| 登录 | 管理员私聊扫码；也可从环境变量或配置读取 Cookie |
| 可靠性 | SQLite 保存订阅、检测游标和待发消息；失败退避重试、跨重启去重、独立收件人状态 |
| 渲染 | Host HTML → 本地 Pillow → 文本回退；作者、正文、图片、转发、统计、原文二维码 |
| 配置 | SDK 配置模型和 WebUI Schema；热重载时停止旧任务、关闭旧连接，再启动新配置 |

### 低延迟的条件

“10 秒”是**关注流的默认检查周期，不是送达时限承诺**。需要有效登录，并且该 B 站账号已经关注对应 UP 主。一次关注流请求覆盖多个 UP，因此不需要每个订阅各发一次高频请求。插件不自动修改 B 站账号的关注关系。

`polling.mode = "auto"` 默认在登录时使用关注流，同时每个 UP 约 300 秒主页补查一次；无登录时只用主页轮询。主页调度默认每 5 秒检查一个 UP，每个 UP 的最小匿名检查间隔是 30 秒；因此完整轮询时间至少约为 `max(30, 5 × UP 数量)` 秒，再加请求耗时。未关注、登录失效、网络异常或风控都会增加延迟。

接口返回 412、429、-352 等风控状态时，插件暂停 API 请求并退避，不尝试绕过验证。直播默认 30 秒批量检查，番剧默认 120 秒检查。

## 安装

1. 从本仓库下载源码，或在 MaiBot 根目录执行 `git clone https://github.com/EzraRT/MaiBot-Bilibili-Dynamic.git plugins/maibot-bilibili-dynamic`。至少保留 `_manifest.json`、`plugin.py`、`bili/`、`LICENSE`、`NOTICE`。如果使用安装包，解压后的这一层目录应直接位于 `plugins/` 下。
2. 启动或重载 MaiBot。Host 会根据 Manifest 安装 Python 依赖；Runner 首次加载后生成插件目录下的 `config.toml`。无需单独运行 `main.py`，它只是离线卡片预览工具。
3. 在 WebUI 配置管理员，例如 `access.admins = ["qq:123456789"]`。填写真实平台名和用户 ID。默认 `allow_public_subscribe = false`；开启后，当前会话的任何成员都可以修改该会话的订阅，但不能扫码登录。
4. 私聊机器人发送 `/bili login`，使用 B 站 App 扫码确认。也可以在启动 MaiBot 前设置环境变量 `BILIBILI_COOKIE`；Cookie 读取顺序为环境变量、`account.cookie` 配置、扫码保存的凭据。
5. 在登录的 B 站账号中关注目标 UP，在需要接收推送的群或私聊发送 `/bili add 目标UID`。
6. 发送 `/bili status` 查看当前聊天流 ID、检测方式和待发送数；分享 B 站链接即可测试卡片。

示例配置见 [config.example.toml](config.example.toml)。**不要把真实 Cookie 写进仓库或发到群聊。** 扫码凭据保存在 Host 分配的数据目录中的 `account.cookie`，POSIX 系统文件权限为 `0600`。扫码登录不会自动刷新过期 Cookie；登录失效时重新扫码。

### 中文字体和图片

默认优先调用 MaiBot 的 `render.html2png`，需要 Host 已配置可用的浏览器渲染环境。如果不可用，自动使用 Pillow。Linux 的 Pillow 渲染需要中文字体，例如 Debian/Ubuntu 的 `fonts-noto-cjk`；也可以把已有字体的路径填入 `cards.font_path`。macOS 和 Windows 会寻找系统中文字体。两种渲染方式都失败时会发送包含原文链接的文本。

图片与原文链接通过 `send.hybrid` 发出；适配器明确返回发送失败时改发文本。卡片仅加载 B 站图片 CDN 资源，HTML 渲染关闭网络访问，外部正文被转义。

## 命令

所有修改只作用于**命令所在会话**，不接受跨群目标参数。管理员可在对应会话操作。

| 命令 | 用途 |
| --- | --- |
| `/bili help` | 帮助，支持 `/bili 帮助` |
| `/bili add 123456` | 订阅该 UP 的动态和直播 |
| `/bili add 123456 dynamic` | 只订阅动态 |
| `/bili add 123456 live` | 只订阅直播 |
| `/bili add ss12345` | 订阅番剧；也接受 ep、md 号 |
| `/bili list` | 查看本会话的订阅和过滤器 |
| `/bili del 123456` | 取消订阅，支持已订阅名称的唯一匹配 |
| `/bili pause 123456` / `/bili resume 123456` | 暂停 / 恢复推送 |
| `/bili clear confirm` | 清空本会话订阅和关联待发消息 |
| `/bili filter 123456 type black 转发,直播结束` | 不推送这些类型 |
| `/bili filter 123456 type white 视频,图文` | 只推送这些类型 |
| `/bili filter 123456 regex black "抽奖\|广告"` | 过滤含关键词的内容 |
| `/bili filter 123456 regex white "新作\|更新"` | 只推送匹配的内容 |
| `/bili filter 123456 clear` | 清空过滤器 |
| `/bili new 123456 3` | 查询最近的 1–3 条可见动态，默认 1 条 |
| `/bili video 123456` | 查询最新视频，使用 WBI 签名接口 |
| `/bili search 1234567890123` | 按动态 ID 查询 |
| `/bili parse https://b23.tv/xxxxxx` | 手动解析，或直接分享链接自动解析 |
| `/bili login` / `/bili logout` | 管理员私聊登录 / 清除扫码凭据 |
| `/bili status` | 检测模式、待发送数量、最近异常、聊天流 ID |

类型可选：`动态`、`图文`、`转发`、`视频`、`专栏`、`音乐`、`番剧`、`直播`、`直播结束`。类型与正则过滤器依次执行，两者都允许才推送；每次设置会替换该订阅同类过滤器。带空格的正则需要引号。正则有执行时间限制，超时则跳过该条推送。

自动解析同一内容在同一会话默认冷却 120 秒，成功发送后才记录；不同会话分别计算。每条消息最多解析 3 个链接。`links.only_when_mentioned` 可以改为仅被提及时解析。

## 数据与恢复

- 持久化目录使用 `ctx.paths.data_dir`，通常为 `data/plugins/com.ezrart.bilibili-dynamic/`。升级请保留整个目录，尤其是 `bilibili.sqlite3` 及 SQLite 尚未合并的 WAL 文件；备份前先停止插件。
- 新增 UP 订阅仅推送订阅时间之后的动态，置顶旧动态不会触发历史刷屏。动态中断后默认最多补查 24 小时，可配置到 7 天。API 返回分页时会保存偏移游标，下轮继续。
- 直播首次成功查询建立基线，之后通知状态变化；短于轮询间隔的开播/下播可能无法观测。番剧以订阅时的可播放集数为基线。
- 已入队的发送失败会独立重试，间隔逐渐增加到最多 15 分钟；暂停会保留待发消息。取消订阅会删除该订阅的待发记录。
- 发送采用“至少一次”语义。正常重启可去重；如果平台已接收消息、进程却在本地确认前崩溃，恢复后仍可能重发。未检测到且超出补查时间范围、或 B 站接口未返回的内容无法保证补回。
- API Cookie 不发送给短链接站点或图片 CDN；短链每一步跳转都校验域名。图片有下载大小和解码尺寸限制。

## 本地开发与验证

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python main.py
uv run python scripts/package.py
```

`main.py` 生成离线示例 PNG 和 HTML，数据为演示内容。测试通过真实安装的 SDK 加载入口和发送代理，使用模拟 B 站响应和 Host RPC；覆盖订阅隔离、动态分页、置顶、去重、持久化重试、直播/番剧基线、过滤器、短链重定向、风控退避、配置重载及渲染回退。测试不会使用真实账号或发送真实消息。

![离线卡片样例](docs/card-preview.png)

自动化测试使用模拟 Host 和 B 站响应；安装后请按上述步骤验证“收到真实动态 → 向实际群聊发图”的完整流程。

## 与原 Mirai 插件的范围差异

本版迁移核心订阅、检测、推送、解析和过滤功能，重新实现 Python 运行时、持久化和卡片渲染。暂未迁移 Mirai 特有的分组/跨通讯录管理、@全体、交互式模板编辑、多账号轮换、Mirai 配置自动导入和番剧追番关系修改。图像排版为重新设计，不是原 Skia 画面的像素级复刻。付费、登录可见或已删除内容仍受 B 站接口实际可见性限制。

## 来源和许可

本项目将 [Colter23/bilibili-dynamic-mirai-plugin](https://github.com/Colter23/bilibili-dynamic-mirai-plugin) 的主要功能迁移到 MaiBot，感谢 Colter23 及原项目贡献者。参考原项目 v3 源码提交 `e07db1c9131b59fbd550fe9042556e20b4d877e2`，以 Python 重新实现运行时、存储、渲染与插件集成，未包含原项目的 Mirai 二进制或字体、插画资源。

延续原项目的 **AGPL-3.0** 许可，详见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。依据 [MaiBot 插件指南](https://docs.mai-mai.org/plugin/)、[事件处理器](https://docs.mai-mai.org/plugin/event-handlers) 和 [能力 API](https://docs.mai-mai.org/plugin/api-reference) 适配新 SDK；接口核对使用 MaiBot 1.3.5 源码提交 `f9ec3670a3638d6a4708ee5253f7ffe2838c9415`，本地验证 SDK 版本为 `maibot-plugin-sdk 2.11.0`。
