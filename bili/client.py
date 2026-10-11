from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import time
from collections import OrderedDict
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
from PIL import Image, ImageOps

from .config import PollingConfig
from .links import PAGE_HOSTS, SHORT_HOSTS, Link, allowed_url, parse_url
from .models import Card, dynamic_card, number, timestamp

API = "https://api.bilibili.com"
LIVE_API = "https://api.live.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
MIXIN_ORDER = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


class BiliError(Exception):
    """A deliberately credential-free error safe to display to the user."""


class RiskControl(BiliError):
    pass


class BiliClient:
    def __init__(self, config: PollingConfig, cookie: str = "", *, transport=None):
        self.config = config
        self.cookie = cookie.strip()
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
        self.http = httpx.AsyncClient(
            headers=headers,
            timeout=config.timeout,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        # Short links / CDN requests must never inherit the account cookies.
        self.public_http = httpx.AsyncClient(
            headers=headers,
            timeout=config.timeout,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self._rate_lock = asyncio.Lock()
        self._asset_limit = asyncio.Semaphore(3)
        self._next_request = 0.0
        self.cooldown_until = 0.0
        self._wbi: tuple[str, float] = ("", 0)
        self._assets: OrderedDict[str, tuple[float, str]] = OrderedDict()

    @property
    def logged_in(self) -> bool:
        return bool(self.cookie_values().get("SESSDATA"))

    def cookie_values(self) -> dict[str, str]:
        jar = SimpleCookie()
        try:
            jar.load(self.cookie)
        except Exception:
            return {}
        return {key: value.value for key, value in jar.items()}

    async def close(self) -> None:
        await self.http.aclose()
        await self.public_http.aclose()

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            if now < self.cooldown_until:
                raise RiskControl(f"B 站请求冷却中，约 {int(self.cooldown_until - now) + 1} 秒后重试")
            await asyncio.sleep(max(0, self._next_request - now))
            if time.monotonic() < self.cooldown_until:
                raise RiskControl("B 站请求冷却中")
            self._next_request = time.monotonic() + self.config.request_gap

    async def request(self, path: str, params=None, *, base: str = API, post: dict | None = None) -> dict:
        if base not in {API, LIVE_API, PASSPORT} or not path.startswith("/"):
            raise BiliError("无效 API 地址")
        for attempt in range(3):
            await self._throttle()
            try:
                response = await self.http.request(
                    "POST" if post is not None else "GET",
                    base + path,
                    params=params,
                    data=post,
                    headers={"Cookie": self.cookie} if self.cookie else {},
                )
                if response.status_code in {412, 429}:
                    self.cooldown_until = time.monotonic() + self.config.risk_backoff
                    raise RiskControl(f"B 站限制请求（HTTP {response.status_code}），已自动退避")
                if response.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                if response.status_code != 200:
                    raise BiliError(f"B 站接口返回 HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise BiliError("B 站接口数据格式异常")
                code = payload.get("code", -1)
                if code == -101 and path == "/x/web-interface/nav":
                    anonymous_nav = payload.get("data") or {}
                    if anonymous_nav.get("wbi_img"):
                        return anonymous_nav
                if code in {-352, -412, -509, -799}:
                    self.cooldown_until = time.monotonic() + self.config.risk_backoff
                    raise RiskControl(f"B 站风控（{code}），已自动退避")
                if code in {-101, -111}:
                    raise BiliError(f"B 站登录已失效或缺少 Cookie（{code}）")
                if code != 0:
                    raise BiliError(f"B 站接口错误（{code}），内容可能不可见或参数不正确")
                return payload.get("data") or payload.get("result") or {}
            except (httpx.RequestError, ValueError):
                if attempt == 2:
                    raise BiliError("B 站网络请求失败或返回非 JSON 数据") from None
                await asyncio.sleep(2**attempt)
        raise BiliError("B 站请求失败")

    async def feed(self, offset: str = "", uid: str = "") -> dict:
        params = {"type": "all", "timezone_offset": "-480", "features": "itemOpusStyle", "offset": offset}
        if uid:
            params["host_mid"] = uid
        data = await self.request(
            "/x/polymer/web-dynamic/v1/feed/space" if uid else "/x/polymer/web-dynamic/v1/feed/all", params
        )
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise BiliError("动态列表数据格式异常")
        return data

    async def dynamic(self, did: str) -> Card:
        data = await self.request(
            "/x/polymer/web-dynamic/v1/detail", {"id": did, "features": "itemOpusStyle"}
        )
        if not data.get("item"):
            raise BiliError("动态不存在或暂不可见")
        return dynamic_card(data["item"])

    async def user(self, uid: str) -> dict:
        data = await self.request("/x/web-interface/card", {"mid": uid})
        card = data.get("card") or {}
        if not card.get("mid"):
            raise BiliError("未找到这个 UP 主")
        return card

    async def video(self, vid: str) -> Card:
        data = await self.request(
            "/x/web-interface/view",
            {"bvid": vid} if vid.startswith("BV") else {"aid": vid.removeprefix("av")},
        )
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        return Card(
            id=data.get("bvid") or vid,
            kind="视频",
            title=data.get("title", "视频"),
            author=owner.get("name", ""),
            uid=str(owner.get("mid", "")),
            text=data.get("desc", ""),
            url=f"https://www.bilibili.com/video/{data.get('bvid') or vid}",
            avatar=owner.get("face", ""),
            images=[data.get("pic", "")],
            timestamp=timestamp(data.get("pubdate")),
            stats=[
                f"播放 {number(stat.get('view'))}",
                f"弹幕 {number(stat.get('danmaku'))}",
                f"赞 {number(stat.get('like'))}",
            ],
        )

    async def article(self, aid: str) -> Card:
        data = await self.request("/x/article/viewinfo", {"id": aid})
        author = data.get("author") or {}
        return Card(
            id=aid,
            kind="专栏",
            title=data.get("title", "专栏"),
            author=author.get("name", ""),
            uid=str(author.get("mid", data.get("mid", ""))),
            avatar=author.get("face", ""),
            text=data.get("summary", ""),
            images=data.get("origin_image_urls") or data.get("image_urls") or [],
            url=f"https://www.bilibili.com/read/cv{aid}",
            timestamp=timestamp(data.get("publish_time")),
        )

    async def live_status(self, uids: list[str]) -> dict:
        return await self.request(
            "/room/v1/Room/get_status_info_by_uids", [("uids[]", uid) for uid in uids], base=LIVE_API
        )

    @staticmethod
    def live_card(data: dict, *, ended: bool = False) -> Card:
        room = str(data.get("room_id", ""))
        start = timestamp(data.get("live_time", 0))
        return Card(
            id=f"{room}:{start}:{'end' if ended else 'start'}",
            kind="直播结束" if ended else "直播",
            title=data.get("title", "直播间"),
            author=data.get("uname", "主播"),
            uid=str(data.get("uid", "")),
            text="直播已结束" if ended else "正在直播" if data.get("live_status") == 1 else "当前未开播",
            avatar=data.get("face", ""),
            images=[data.get("cover_from_user") or data.get("user_cover") or data.get("keyframe") or ""],
            timestamp=start,
            url=f"https://live.bilibili.com/{room}",
            stats=[data.get("area_v2_name") or data.get("area_name") or ""],
        )

    async def live(self, rid: str) -> Card:
        data = await self.request("/room/v1/Room/get_info", {"room_id": rid}, base=LIVE_API)
        try:
            author = await self.user(str(data["uid"]))
            data.update(uname=author.get("name", ""), face=author.get("face", ""))
        except BiliError:
            pass
        return self.live_card(data)

    async def season(self, sid: str) -> dict:
        if sid.startswith("md"):
            media = await self.request("/pgc/review/user", {"media_id": sid[2:]})
            season_id = (media.get("media") or {}).get("season_id")
            if not season_id:
                raise BiliError("未找到此番剧")
            sid = f"ss{season_id}"
        key = "ep_id" if sid.startswith("ep") else "season_id"
        data = await self.request("/pgc/view/web/season", {key: sid[2:]})
        if not data.get("season_id"):
            raise BiliError("未找到此番剧")
        return data

    @staticmethod
    def season_card(data: dict, episode: dict | None = None) -> Card:
        ep = episode or {}
        title = data.get("title", "番剧")
        subtitle = " ".join(str(ep.get(x) or "") for x in ("title", "long_title")).strip()
        return Card(
            id=f"ep{ep['id']}" if ep.get("id") else f"ss{data['season_id']}",
            kind="番剧",
            title=f"{title} · {subtitle}" if subtitle else title,
            author=title,
            uid=f"ss{data['season_id']}",
            text=ep.get("share_copy") or data.get("evaluate", ""),
            url=f"https://www.bilibili.com/bangumi/play/ep{ep['id']}"
            if ep.get("id")
            else f"https://www.bilibili.com/bangumi/play/ss{data['season_id']}",
            images=[ep.get("cover") or data.get("cover", "")],
            timestamp=timestamp(ep.get("pub_time", 0)),
        )

    async def signed(self, params: dict) -> dict:
        if time.monotonic() > self._wbi[1]:
            data = await self.request("/x/web-interface/nav")
            wbi = data.get("wbi_img") or {}
            raw = "".join(
                urlparse(wbi.get(k, "")).path.rsplit("/", 1)[-1].split(".")[0] for k in ("img_url", "sub_url")
            )
            if len(raw) < 64:
                raise BiliError("无法获取 WBI 签名信息")
            self._wbi = ("".join(raw[i] for i in MIXIN_ORDER)[:32], time.monotonic() + 3600)
        params = {**params, "wts": int(time.time())}
        params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(params.items())}
        params["w_rid"] = hashlib.md5((urlencode(params) + self._wbi[0]).encode()).hexdigest()
        return params

    async def latest_video(self, uid: str) -> Card:
        params = await self.signed({"mid": uid, "ps": 1, "pn": 1, "order": "pubdate"})
        data = await self.request("/x/space/wbi/arc/search", params)
        videos = (data.get("list") or {}).get("vlist") or []
        if not videos:
            raise BiliError("该 UP 暂无公开视频")
        return await self.video(videos[0]["bvid"])

    async def resolve_short(self, url: str) -> Link:
        for _ in range(5):
            if not allowed_url(url, SHORT_HOSTS | PAGE_HOSTS):
                raise BiliError("短链接跳转到了不支持的站点")
            parsed = parse_url(url)
            if parsed and parsed.kind != "short":
                return parsed
            if urlparse(url).hostname not in SHORT_HOSTS:
                raise BiliError("这个 B 站页面暂不支持解析")
            try:
                async with self.public_http.stream("GET", url) as response:
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        raise BiliError("短链接没有返回有效跳转")
                    location = response.headers.get("location", "")
                    if not location:
                        raise BiliError("短链接缺少跳转目标")
                    url = urljoin(url, location)
            except httpx.RequestError:
                raise BiliError("短链接请求失败") from None
        raise BiliError("短链接跳转次数过多")

    async def resolve(self, link: Link) -> Card:
        if link.kind == "short":
            link = await self.resolve_short(link.id)
        if link.kind == "dynamic":
            return await self.dynamic(link.id)
        if link.kind == "video":
            return await self.video(link.id)
        if link.kind == "article":
            return await self.article(link.id)
        if link.kind == "live":
            return await self.live(link.id)
        if link.kind == "season":
            data = await self.season(link.id)
            ep = next((ep for ep in data.get("episodes", []) if f"ep{ep['id']}" == link.id), None)
            return self.season_card(data, ep)
        if link.kind == "user":
            data = await self.user(link.id)
            return Card(
                id=link.id,
                kind="用户",
                title=data.get("name", "UP 主"),
                author=data.get("name", ""),
                uid=link.id,
                avatar=data.get("face", ""),
                text=data.get("sign", ""),
                url=f"https://space.bilibili.com/{link.id}",
                stats=[f"粉丝 {number(data.get('fans'))}"],
            )
        raise BiliError("不支持的链接类型")

    async def qr_generate(self) -> dict:
        return await self.request("/x/passport-login/web/qrcode/generate", base=PASSPORT)

    async def qr_poll(self, key: str) -> tuple[int, str]:
        data = await self.request("/x/passport-login/web/qrcode/poll", {"qrcode_key": key}, base=PASSPORT)
        code = int(data.get("code", -1))
        if code != 0:
            return code, ""
        values = {k: v[-1] for k, v in parse_qs(urlparse(data.get("url", "")).query).items()}
        for cookie in self.http.cookies.jar:
            if cookie.domain.lstrip(".") in {"bilibili.com", "passport.bilibili.com"}:
                values[cookie.name] = cookie.value
        names = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
        if not values.get("SESSDATA"):
            raise BiliError("扫码成功但未返回有效登录凭据")
        return code, "; ".join(f"{k}={values[k]}" for k in names if values.get(k))

    async def image_data(self, url: str) -> str:
        """Fetch only Bilibili image CDN assets, bound size, then decode/re-encode as PNG."""
        if url.startswith("//"):
            url = "https:" + url
        p = urlparse(url)
        host = p.hostname or ""
        if not (host == "hdslb.com" or host.endswith(".hdslb.com")) or not allowed_url(url, {host}):
            return ""
        if cached := self._assets.get(url):
            if cached[0] > time.monotonic():
                self._assets.move_to_end(url)
                return cached[1]
            del self._assets[url]
        async with self._asset_limit:
            try:
                async with self.public_http.stream("GET", url) as response:
                    if response.status_code != 200:
                        return ""
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 8 * 1024 * 1024:
                            return ""
                data = await asyncio.to_thread(self._normalise_image, bytes(content))
            except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError):
                return ""
        self._assets[url] = (time.monotonic() + 600, data)
        while len(self._assets) > 32:
            self._assets.popitem(last=False)
        return data

    @staticmethod
    def _normalise_image(content: bytes) -> str:
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > 20_000_000:
                return ""
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1200, 1200))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
