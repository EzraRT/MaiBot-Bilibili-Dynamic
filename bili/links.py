from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

SHORT_HOSTS = {"b23.tv", "bili2233.cn"}
PAGE_HOSTS = {
    "www.bilibili.com",
    "bilibili.com",
    "m.bilibili.com",
    "t.bilibili.com",
    "live.bilibili.com",
    "space.bilibili.com",
}
URL_RE = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:www\.|m\.|t\.|live\.|space\.)?"
    r"(?:bilibili\.com|b23\.tv|bili2233\.cn)/[^\s<>\"'，。！？；、（）【】]+",
    re.I,
)
ID_RE = re.compile(r"(?<![\w/])(?:BV[0-9A-Za-z]{10}|av\d+|cv\d+|(?:ss|ep|md)\d+)(?!\w)")


@dataclass(frozen=True)
class Link:
    kind: str
    id: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


def allowed_url(url: str, hosts: set[str]) -> bool:
    try:
        p = urlparse(url)
        return (
            p.scheme in {"http", "https"}
            and p.hostname in hosts
            and p.username is None
            and p.password is None
            and p.port in {None, 80, 443}
        )
    except ValueError:
        return False


def parse_url(url: str) -> Link | None:
    if "://" not in url:
        url = "https://" + url
    if not allowed_url(url, PAGE_HOSTS | SHORT_HOSTS):
        return None
    p = urlparse(url)
    path = p.path.rstrip("/")
    if p.hostname in SHORT_HOSTS:
        return Link("short", "https://" + p.hostname + path) if re.fullmatch(r"/[A-Za-z0-9]+", path) else None
    patterns = []
    if p.hostname in {"t.bilibili.com", "m.bilibili.com"}:
        patterns.append(("dynamic", r"/(?:dynamic/)?(\d+)"))
    if p.hostname in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}:
        patterns.extend(
            [
                ("dynamic", r"/opus/(\d+)"),
                ("video", r"/video/(BV[0-9A-Za-z]{10}|av\d+)"),
                ("article", r"/read/(?:cv|mobile/)(\d+)"),
                ("season", r"/bangumi/(?:play|media)/((?:ss|ep|md)\d+)"),
            ]
        )
    if p.hostname == "live.bilibili.com":
        patterns.append(("live", r"/(?:h5/)?(\d+)"))
    if p.hostname == "space.bilibili.com":
        patterns.append(("user", r"/(\d+)(?:/dynamic)?"))
    for kind, pattern in patterns:
        if m := re.fullmatch(pattern, path):
            return Link(kind, m[1])
    return None


def extract_links(text: str, *, bare_ids: bool = True, limit: int = 3) -> list[Link]:
    text = text[:32000].replace("\\/", "/").replace("&amp;", "&")
    matches: list[tuple[int, Link]] = []
    spans = []
    for match in URL_RE.finditer(text):
        spans.append(match.span())
        if link := parse_url(match[0].rstrip(".,;!?)]}")):
            matches.append((match.start(), link))
    if bare_ids:
        for match in ID_RE.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            value = match[0]
            kind = (
                "video"
                if value.startswith(("BV", "av"))
                else "article"
                if value.startswith("cv")
                else "season"
            )
            matches.append((match.start(), Link(kind, value[2:] if kind == "article" else value)))
    result = []
    for _, link in sorted(matches, key=lambda x: x[0]):
        if link not in result:
            result.append(link)
        if len(result) >= limit:
            break
    return result


def message_text(message: dict) -> str:
    """Read the SDK's serialized message segments, including JSON share cards."""
    raw = message.get("raw_message", "")
    parts = [message.get("processed_plain_text") or ""]
    if isinstance(raw, str):
        parts.append(raw)
    elif isinstance(raw, list):
        for segment in raw[:100]:
            if not isinstance(segment, dict) or segment.get("type") not in {"text", "json", "xml", "app"}:
                continue
            data = segment.get("data", segment.get("content", ""))
            parts.append(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    return "\n".join(str(x)[:32000] for x in parts)[:64000]
