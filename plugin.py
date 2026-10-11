"""MaiBot SDK entry point. Runner loads this module as an isolated package."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import time
from collections import OrderedDict
from pathlib import Path

import regex
from maibot_sdk import Command, EventHandler, MaiBotPlugin
from maibot_sdk.types import EventType

from .bili.client import BiliClient, BiliError
from .bili.config import BiliConfig
from .bili.links import extract_links, message_text
from .bili.models import Card, dynamic_card
from .bili.render import Renderer, qr_png
from .bili.service import Monitor
from .bili.store import Store

HELP = """Bilibili 订阅与卡片
/bili add <UID|ss号|ep号|md号> [all|dynamic|live] 订阅
/bili del <UID|ss号|名称> 取消当前会话订阅
/bili list 查看订阅；/bili clear confirm 清空当前会话
/bili pause <ID> 暂停；/bili resume <ID> 恢复
/bili filter <ID> type <black|white> <类型,类型>
/bili filter <ID> regex <black|white> "正则表达式"
/bili filter <ID> clear 清空过滤器
类型：动态、图文、转发、视频、专栏、音乐、番剧、直播、直播结束
/bili new <UID> [1-3] 最新动态；/bili video <UID> 最新视频
/bili search <动态ID>；/bili parse <链接或BV号>
/bili login 私聊扫码登录；/bili logout 清除扫码登录
/bili status 状态与当前聊天流 ID
订阅管理默认仅限 access.admins 中的管理员。
低延迟动态流需要登录并在该 B 站账号中关注对应 UP。"""

ALIASES = {
    "帮助": "help",
    "h": "help",
    "menu": "help",
    "添加": "add",
    "订阅": "add",
    "删除": "del",
    "取消订阅": "del",
    "列表": "list",
    "登录": "login",
    "状态": "status",
    "最新动态": "new",
    "最新视频": "video",
    "搜索": "search",
    "解析": "parse",
    "暂停": "pause",
    "恢复": "resume",
    "过滤": "filter",
}
MUTATIONS = {"add", "del", "clear", "pause", "resume", "filter"}
KINDS = {"动态", "图文", "转发", "视频", "专栏", "音乐", "番剧", "直播", "直播结束"}


class BilibiliDynamicPlugin(MaiBotPlugin):
    config_model = BiliConfig

    def __init__(self):
        super().__init__()
        self._runtime_lock = asyncio.Lock()
        self._login_task: asyncio.Task | None = None
        self._link_tasks: set[asyncio.Task] = set()
        self._command_tasks: set[asyncio.Task] = set()
        self._cooldowns: OrderedDict[tuple[str, str], float] = OrderedDict()
        self.store: Store | None = None
        self.client: BiliClient | None = None
        self.monitor: Monitor | None = None
        self.renderer: Renderer | None = None
        self.settings = BiliConfig()
        self._closing = True

    async def on_load(self) -> None:
        async with self._runtime_lock:
            self.settings = self.config
            self.store = Store(Path(self.ctx.paths.data_dir) / "bilibili.sqlite3")
            await self.store.open()
            try:
                await self._start_runtime()
            except BaseException:
                await self.store.close()
                raise

    async def _start_runtime(self) -> None:
        account = self.settings.account
        cookie = os.environ.get(account.cookie_env, "").strip() or account.cookie.strip()
        if not cookie:
            file = Path(self.ctx.paths.data_dir) / "account.cookie"
            if file.exists():
                cookie = await asyncio.to_thread(file.read_text, encoding="utf-8")
        self.client = BiliClient(self.settings.polling, cookie)
        self.renderer = Renderer(self.settings.cards, self.client, self.ctx)
        self.monitor = Monitor(self.store, self.client, self.settings, self._send_card, self.ctx.logger)
        self._closing = False
        if self.settings.plugin.enabled:
            self.monitor.start()
        self.ctx.logger.info(
            "Bilibili 插件已加载；模式=%s，登录凭据=%s",
            self.settings.polling.mode,
            "已配置" if self.client.logged_in else "未配置",
        )

    async def _stop_runtime(self) -> None:
        await self._cancel_interactions()
        if self.monitor:
            await self.monitor.stop()
        if self.client:
            await self.client.close()

    async def _cancel_interactions(self) -> None:
        self._closing = True
        tasks = [*self._link_tasks, *self._command_tasks]
        if self._login_task:
            tasks.append(self._login_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._link_tasks.clear()
        self._command_tasks.clear()
        self._login_task = None

    async def on_unload(self) -> None:
        # Cancel requests before taking the lock; a slow link must not block plugin shutdown.
        await self._cancel_interactions()
        async with self._runtime_lock:
            await self._stop_runtime()
            if self.store:
                await self.store.close()
                self.store = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        # Validate before stopping the working runtime; a bad config must not take it down.
        settings = BiliConfig.model_validate(config_data)
        await self._cancel_interactions()
        async with self._runtime_lock:
            await self._stop_runtime()
            self.set_plugin_config(config_data)
            self.settings = settings
            self._cooldowns.clear()
            await self._start_runtime()

    def _allowed(self, stream_id: str) -> bool:
        streams = self.settings.access.allowed_streams
        return bool(stream_id) and (not streams or stream_id in streams)

    def _admin(self, kwargs: dict) -> bool:
        if kwargs.get("is_local_operator") is True:
            return True
        message = kwargs.get("message") or {}
        info = message.get("message_info") or {}
        user = info.get("user_info") or message.get("user_info") or {}
        platform = str(kwargs.get("platform") or message.get("platform") or "")
        user_id = str(kwargs.get("user_id") or user.get("user_id") or "")
        return bool(platform and user_id and f"{platform}:{user_id}" in self.settings.access.admins)

    async def _send_card(self, stream_id: str, card: Card) -> bool:
        image = await self.renderer.render(card)
        if image:
            ok = await self.ctx.send.hybrid(
                [
                    {"type": "image", "content": image},
                    {"type": "text", "content": card.url},
                ],
                stream_id,
            )
            if ok:
                return True
        return bool(await self.ctx.send.text(card.plain_text(), stream_id))

    @Command(
        "bili",
        description="Bilibili 订阅管理与卡片",
        pattern=r"^/bili(?:\s+(?P<args>[\s\S]*))?$",
        timeout_ms=120000,
    )
    async def handle_command(self, **kwargs):
        if self._closing:
            return False, "插件正在重载", 2
        task = asyncio.current_task()
        self._command_tasks.add(task)
        try:
            return await self._handle_command(**kwargs)
        finally:
            self._command_tasks.discard(task)

    async def _handle_command(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "")
        if not self._allowed(stream_id):
            return False, "当前会话未启用 Bilibili 插件", 2
        raw = (kwargs.get("matched_groups") or {}).get("args")
        if raw is None:
            text = (
                kwargs.get("text") or kwargs.get("raw_message") or message_text(kwargs.get("message") or {})
            )
            raw = re.sub(r"^/bili\s*", "", str(text), count=1)
        try:
            args = shlex.split(raw or "")
        except ValueError:
            await self.ctx.send.text("参数引号不匹配，请把含空格的参数放进一对双引号中。", stream_id)
            return False, "参数错误", 2
        action = ALIASES.get(args[0], args[0]) if args else "help"
        args = args[1:]
        if action in MUTATIONS and not (self._admin(kwargs) or self.settings.access.allow_public_subscribe):
            await self.ctx.send.text(
                "仅管理员可修改订阅；请在 access.admins 配置 platform:user_id。", stream_id
            )
            return False, "没有订阅管理权限", 2
        if action in {"login", "logout"}:
            info = (kwargs.get("message") or {}).get("message_info") or {}
            if not self._admin(kwargs) or kwargs.get("group_id") or info.get("group_info"):
                await self.ctx.send.text("登录管理仅限已配置的管理员在私聊中使用。", stream_id)
                return False, "需要管理员私聊", 2
        async with self._runtime_lock:
            if self._closing or not self.store:
                return False, "插件正在重载", 2
            try:
                async with asyncio.timeout(110):
                    reply = await self._command(action, args, stream_id)
                if reply:
                    await self.ctx.send.text(reply, stream_id)
                return True, "已处理", 2
            except (BiliError, ValueError) as exc:
                # Pydantic/config validation is not handled here: it can contain credential fields.
                await self.ctx.send.text(str(exc), stream_id)
                return False, "请求未完成", 2
            except Exception as exc:
                self.ctx.logger.warning("Bilibili 命令失败: %s", type(exc).__name__)
                await self.ctx.send.text("请求暂未完成，请稍后重试；可用 /bili status 检查状态。", stream_id)
                return False, "请求失败", 2

    @staticmethod
    def _uid(value: str) -> str:
        if not re.fullmatch(r"[1-9]\d{0,19}", value):
            raise ValueError("UID 应为正整数，例如 /bili add 123456")
        return value

    async def _subject(self, stream_id: str, value: str) -> str:
        subs = await self.store.subscriptions(stream_id)
        exact = [s for s in subs if s.subject == value or s.name == value]
        matches = exact or [s for s in subs if value.casefold() in s.name.casefold()]
        if not matches:
            raise ValueError("当前会话没有这个订阅，请先用 /bili list 查看")
        if len(matches) != 1:
            raise ValueError("匹配到多个订阅，请使用准确的 UID 或 ss 号")
        return matches[0].subject

    async def _command(self, action: str, args: list[str], stream_id: str) -> str:
        if action == "help":
            return HELP
        if action == "status":
            pending = await self.store.pending_count(stream_id)
            logged = self.client.logged_in
            mode = self.settings.polling.mode
            actual = (
                "关注流 + 主页补查"
                if mode == "auto" and logged
                else "逐 UID 主页轮询"
                if mode in {"auto", "space"}
                else "关注流"
                if logged
                else "关注流不可用（未登录）"
            )
            errors = [
                f"{name}: {data['error']}" for name, data in self.monitor.health.items() if not data["ok"]
            ]
            return (
                f"Bilibili 插件：{'启用' if self.settings.plugin.enabled else '停用'}\n"
                f"聊天流 ID：{stream_id}\n"
                f"登录凭据：{'已配置（有效性以接口响应为准）' if logged else '未配置'}\n"
                f"动态检测：{actual}\n关注流间隔：{self.settings.polling.feed_interval:g} 秒\n"
                f"本会话待发送：{pending}\n"
                + ("最近异常：\n" + "\n".join(errors) if errors else "暂无已记录异常")
            )
        if not self.settings.plugin.enabled:
            raise ValueError("插件已停用，请在配置中启用 plugin.enabled")
        if action == "list":
            subs = await self.store.subscriptions(stream_id)
            if not subs:
                return "当前会话没有订阅。使用 /bili add <UID> 添加。"
            lines = ["当前会话订阅："]
            for s in subs:
                opts = s.options
                modes = [
                    label for key, label in (("dynamic", "动态"), ("live", "直播")) if opts.get(key, True)
                ]
                if s.source == "season":
                    modes = ["番剧"]
                filters = "; ".join(
                    f"{k}={opts[k]}" for k in ("types", "type_mode", "regex", "regex_mode") if opts.get(k)
                )
                lines.append(
                    f"{s.subject} · {s.name} · {'/'.join(modes)}{' [暂停]' if opts.get('paused') else ''}"
                    + (f"\n  {filters}" if filters else "")
                )
            return "\n".join(lines)
        if action == "add":
            if not 1 <= len(args) <= 2:
                raise ValueError("用法：/bili add <UID|ss号|ep号|md号> [all|dynamic|live]")
            subject = args[0]
            mode = args[1] if len(args) == 2 else "all"
            if mode not in {"all", "dynamic", "live"}:
                raise ValueError("订阅类型只能是 all、dynamic 或 live")
            created_at = time.time()
            options = {"dynamic": mode != "live", "live": mode != "dynamic"}
            if re.fullmatch(r"(?:ss|ep|md)[1-9]\d{0,14}", subject):
                if mode == "live":
                    raise ValueError("番剧不能选择 live 模式")
                data = await self.client.season(subject)
                subject, source, name = f"ss{data['season_id']}", "season", data["title"]
                baseline = [
                    str(ep["id"])
                    for ep in data.get("episodes", [])
                    if ep.get("id") and ep.get("aid") and ep.get("cid")
                ]
                options["episode_baseline"] = baseline
                if await self.store.get_state(f"season:{subject}") is None:
                    await self.store.record([], state={f"season:{subject}": baseline})
            else:
                subject = self._uid(subject)
                data = await self.client.user(subject)
                source, name = "user", data.get("name", subject)
            added = await self.store.add(
                stream_id,
                source,
                subject,
                name,
                options=options,
                created_at=created_at,
                limit=self.settings.access.max_subscriptions_per_stream,
            )
            if not added:
                return f"已订阅 {name}（{subject}）。如需改变类型，请先取消再添加。"
            return f"已订阅 {name}（{subject}），只推送订阅后的更新。" + (
                "\n要使用关注流低延迟检测，请登录并在该 B 站账号中关注此 UP；主页轮询会补查。"
                if source == "user"
                else ""
            )
        if action in {"del", "pause", "resume"}:
            if len(args) != 1:
                raise ValueError(f"用法：/bili {action} <UID|ss号|名称>")
            subject = await self._subject(stream_id, args[0])
            if action == "del":
                await self.store.remove(stream_id, subject)
                return f"已取消 {subject} 的订阅。"
            await self.store.update_options(stream_id, subject, {"paused": action == "pause"})
            return f"已{'暂停' if action == 'pause' else '恢复'} {subject} 的推送。"
        if action == "clear":
            if args != ["confirm"]:
                raise ValueError("清空当前会话的所有订阅：/bili clear confirm")
            count = await self.store.clear(stream_id)
            return f"已清空当前会话的 {count} 个订阅。"
        if action == "filter":
            return await self._filter(args, stream_id)
        if action in {"new", "video"}:
            if not args or len(args) > (2 if action == "new" else 1):
                raise ValueError(f"用法：/bili {action} <UID>" + (" [1-3]" if action == "new" else ""))
            uid = self._uid(args[0])
            if action == "video":
                cards = [await self.client.latest_video(uid)]
            else:
                if len(args) > 1 and not args[1].isdigit():
                    raise ValueError("动态数量应为 1 到 3")
                count = int(args[1]) if len(args) > 1 else 1
                if not 1 <= count <= 3:
                    raise ValueError("动态数量应为 1 到 3")
                data = await self.client.feed(uid=uid)
                cards = sorted(
                    (dynamic_card(item) for item in data.get("items", [])),
                    key=lambda card: card.timestamp,
                    reverse=True,
                )[:count]
            if not cards:
                return "没有找到可见的动态。"
            for card in cards:
                if not await self._send_card(stream_id, card):
                    raise BiliError("消息发送失败，请稍后重试")
            return ""
        if action == "search":
            if len(args) != 1 or not re.fullmatch(r"\d{1,30}", args[0]):
                raise ValueError("用法：/bili search <动态ID>")
            card = await self.client.dynamic(args[0])
            if not await self._send_card(stream_id, card):
                raise BiliError("消息发送失败")
            return ""
        if action == "parse":
            links = extract_links(" ".join(args), limit=self.settings.links.max_per_message)
            if not links:
                raise ValueError("没有找到支持的 B 站链接或 BV/av/cv/ss/ep/md 号")
            for link in links:
                card = await self.client.resolve(link)
                if not await self._send_card(stream_id, card):
                    raise BiliError("消息发送失败")
            return ""
        if action == "login":
            return await self._login(stream_id)
        if action == "logout":
            if os.environ.get(self.settings.account.cookie_env) or self.settings.account.cookie:
                raise ValueError("当前使用环境变量或配置中的 Cookie，请移除后重载配置。")
            if self._login_task:
                self._login_task.cancel()
                await asyncio.gather(self._login_task, return_exceptions=True)
                self._login_task = None
            await asyncio.to_thread(
                (Path(self.ctx.paths.data_dir) / "account.cookie").unlink, missing_ok=True
            )
            self.client.cookie = ""
            self.client.http.cookies.clear()
            return "已清除本地扫码登录凭据。"
        raise ValueError("未知命令。使用 /bili help 查看帮助。")

    async def _filter(self, args: list[str], stream_id: str) -> str:
        if len(args) < 2:
            raise ValueError('用法：/bili filter <ID> type|regex <black|white> "规则"；或 <ID> clear')
        subject = await self._subject(stream_id, args[0])
        if args[1:] == ["clear"]:
            changes = {"types": [], "regex": "", "type_mode": "black", "regex_mode": "black"}
        elif len(args) == 4 and args[2] in {"black", "white"}:
            if args[1] == "type":
                types = args[3].replace("，", ",").split(",")
                if not all(kind in KINDS for kind in types):
                    raise ValueError("类型可选：" + "、".join(sorted(KINDS)))
                changes = {"types": types, "type_mode": args[2]}
            elif args[1] == "regex":
                if len(args[3]) > 300:
                    raise ValueError("正则表达式最长 300 字")
                try:
                    regex.compile(args[3])
                except regex.error:
                    raise ValueError("正则表达式语法错误") from None
                changes = {"regex": args[3], "regex_mode": args[2]}
            else:
                raise ValueError("过滤器类型只能是 type 或 regex")
        else:
            raise ValueError('用法：/bili filter <ID> type|regex <black|white> "规则"')
        await self.store.update_options(stream_id, subject, changes)
        return f"已更新 {subject} 的过滤器。使用 /bili list 查看。"

    async def _login(self, stream_id: str) -> str:
        if os.environ.get(self.settings.account.cookie_env) or self.settings.account.cookie:
            raise ValueError("环境变量或配置已提供 Cookie。请先清空它们，才能切换到扫码登录。")
        if self._login_task and not self._login_task.done():
            return "已有登录二维码等待扫描，请先完成或等待其过期。"
        data = await self.client.qr_generate()
        png = await asyncio.to_thread(qr_png, data["url"])
        if not await self.ctx.send.image(base64.b64encode(png).decode(), stream_id):
            raise BiliError("登录二维码发送失败")
        self._login_task = asyncio.create_task(
            self._wait_login(data["qrcode_key"], stream_id), name="bili:login"
        )
        return "请使用哔哩哔哩 App 扫码并确认登录，二维码约 3 分钟内有效。"

    async def _wait_login(self, key: str, stream_id: str) -> None:
        try:
            async with asyncio.timeout(185):
                while True:
                    await asyncio.sleep(3)
                    code, cookie = await self.client.qr_poll(key)
                    if code == 0:
                        await asyncio.to_thread(self._save_cookie, cookie)
                        self.client.cookie = cookie
                        await self.ctx.send.text(
                            "登录成功。请在这个 B 站账号中关注已订阅的 UP，关注流检测会自动启用。", stream_id
                        )
                        return
                    if code == 86038:
                        await self.ctx.send.text("二维码已过期，请重新发送 /bili login。", stream_id)
                        return
                    if code not in {86101, 86090}:
                        raise BiliError("扫码登录返回未知状态")
        except TimeoutError:
            await self.ctx.send.text("等待扫码超时，请重新发送 /bili login。", stream_id)
        except BiliError as exc:
            await self.ctx.send.text(str(exc), stream_id)
        except Exception as exc:
            self.ctx.logger.warning("Bilibili 扫码登录失败: %s", type(exc).__name__)

    def _save_cookie(self, cookie: str) -> None:
        path = Path(self.ctx.paths.data_dir) / "account.cookie"
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(cookie)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @EventHandler(
        "bili_links",
        description="解析聊天中的 B 站链接",
        event_type=EventType.ON_MESSAGE,
        intercept_message=False,
    )
    async def handle_message(self, message=None, **kwargs):
        message = message or {}
        cfg = self.settings
        if self._closing or not cfg.plugin.enabled or not cfg.links.enabled:
            return
        stream_id = str(
            kwargs.get("stream_id") or message.get("session_id") or message.get("stream_id") or ""
        )
        if not self._allowed(stream_id) or message.get("is_command"):
            return
        if cfg.links.only_when_mentioned and not (message.get("is_mentioned") or message.get("is_at")):
            return
        text = message_text(message)
        if text.strip().startswith("/bili"):
            return
        links = extract_links(text, bare_ids=cfg.links.bare_ids, limit=cfg.links.max_per_message)
        if not links or len(self._link_tasks) >= 8:
            return
        task = asyncio.create_task(self._process_links(stream_id, links), name="bili:link-card")
        self._link_tasks.add(task)
        task.add_done_callback(self._link_tasks.discard)

    async def _process_links(self, stream_id: str, links) -> None:
        async with self._runtime_lock:
            if self._closing:
                return
            for link in links:
                try:
                    now = time.monotonic()
                    if self._cooldowns.get((stream_id, link.key), 0) > now:
                        continue
                    async with asyncio.timeout(90):
                        card = await self.client.resolve(link)
                        if self._cooldowns.get((stream_id, card.key), 0) > now:
                            self._remember(stream_id, link.key)
                            continue
                        if await self._send_card(stream_id, card):
                            self._remember(stream_id, link.key)
                            self._remember(stream_id, card.key)
                except Exception as exc:
                    # Parsing a chat link must not interrupt normal conversation or spam an error reply.
                    detail = str(exc) if isinstance(exc, BiliError) else type(exc).__name__
                    self.ctx.logger.warning("Bilibili 链接解析失败: %s", detail)

    def _remember(self, stream_id: str, key: str) -> None:
        pair = (stream_id, key)
        self._cooldowns[pair] = time.monotonic() + self.settings.links.cooldown
        self._cooldowns.move_to_end(pair)
        while len(self._cooldowns) > 2048:
            self._cooldowns.popitem(last=False)


def create_plugin() -> BilibiliDynamicPlugin:
    return BilibiliDynamicPlugin()
