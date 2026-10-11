from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import regex


@dataclass
class Card:
    id: str
    kind: str
    title: str
    author: str = ""
    uid: str = ""
    text: str = ""
    url: str = ""
    timestamp: int = 0
    avatar: str = ""
    images: list[str] = field(default_factory=list)
    stats: list[str] = field(default_factory=list)
    original: Card | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def searchable_text(self) -> str:
        return "\n".join([self.title, self.text, self.original.searchable_text if self.original else ""])

    def plain_text(self) -> str:
        lines = [f"【{self.kind}】{self.title}", self.author, self.text[:1600]]
        if self.original:
            lines.append(f"转发自 {self.original.author}：{self.original.searchable_text[:600]}")
        if self.stats:
            lines.append(" · ".join(self.stats))
        lines.append(self.url)
        return "\n".join(line for line in lines if line)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> Card:
        data = dict(data)
        if data.get("original"):
            data["original"] = cls.from_dict(data["original"])
        return cls(**data)


@dataclass
class Subscription:
    id: int
    stream_id: str
    source: str
    subject: str
    name: str
    created_at: float
    options: dict[str, Any] = field(default_factory=dict)

    def accepts(self, card: Card) -> bool:
        if self.options.get("paused"):
            return False
        if card.kind in {"直播", "直播结束"}:
            if not self.options.get("live", True):
                return False
        elif not self.options.get("dynamic", True):
            return False
        types = self.options.get("types", [])
        if types:
            matches = card.kind in types
            if matches == (self.options.get("type_mode", "black") == "black"):
                return False
        pattern = self.options.get("regex", "")
        if pattern:
            try:
                matches = bool(regex.search(pattern, card.searchable_text[:12000], timeout=0.03))
            except (regex.error, TimeoutError):
                # A timed-out filter must never flood the subscribed chat.
                return False
            if matches == (self.options.get("regex_mode", "black") == "black"):
                return False
        return True


def number(value: Any) -> str:
    try:
        n = int(value or 0)
    except (ValueError, TypeError):
        return str(value or "0")
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def timestamp(value: Any) -> int:
    if isinstance(value, (int, float)) or str(value).isdigit():
        return int(value)
    try:
        from datetime import timedelta

        return int(
            datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone(timedelta(hours=8)))
            .timestamp()
        )
    except (ValueError, TypeError):
        return 0


def dynamic_card(item: dict, depth: int = 0) -> Card:
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    body = modules.get("module_dynamic") or {}
    major = body.get("major") or {}
    desc = (body.get("desc") or {}).get("text", "")
    kind = {
        "DYNAMIC_TYPE_AV": "视频",
        "DYNAMIC_TYPE_WORD": "动态",
        "DYNAMIC_TYPE_DRAW": "图文",
        "DYNAMIC_TYPE_FORWARD": "转发",
        "DYNAMIC_TYPE_ARTICLE": "专栏",
        "DYNAMIC_TYPE_MUSIC": "音乐",
        "DYNAMIC_TYPE_PGC": "番剧",
        "DYNAMIC_TYPE_LIVE_RCMD": "直播",
        "DYNAMIC_TYPE_LIVE": "直播",
    }.get(item.get("type"), "动态")
    title = "发布了新动态"
    images: list[str] = []
    if opus := major.get("opus"):
        title = opus.get("title") or title
        desc = (opus.get("summary") or {}).get("text") or desc
        images = [p.get("url", "") for p in opus.get("pics", [])]
    if draw := major.get("draw"):
        images = [p.get("src", "") for p in draw.get("items", [])]
    for key in ("archive", "article", "music", "pgc", "common", "courses", "medialist"):
        if content := major.get(key):
            title = content.get("title") or title
            summary = content.get("desc") or content.get("summary") or ""
            if summary and summary != desc:
                desc = f"{desc}\n{summary}".strip()
            covers = content.get("covers") or [content.get("cover", "")]
            images = [x for x in covers if isinstance(x, str) and x]
            break
    if blocked := major.get("blocked"):
        desc = desc or blocked.get("hint_message", "内容暂不可见")
    stat = modules.get("module_stat") or {}
    stats = [
        f"{label} {number((stat.get(key) or {}).get('count'))}"
        for key, label in (("like", "赞"), ("comment", "评论"), ("forward", "转发"))
        if key in stat
    ]
    did = str(item.get("id_str") or item.get("id") or "")
    if not did:
        raise ValueError("动态缺少 ID")
    return Card(
        id=did,
        kind=kind,
        title=title,
        author=author.get("name", "未知作者"),
        uid=str(author.get("mid", "")),
        text=desc,
        url=f"https://t.bilibili.com/{did}",
        timestamp=timestamp(author.get("pub_ts", 0)),
        avatar=author.get("face", ""),
        images=images,
        stats=stats,
        original=(
            dynamic_card(item["orig"], depth + 1)
            if depth < 2 and item.get("orig") and (item["orig"].get("id_str") or item["orig"].get("id"))
            else Card(id="deleted", kind="动态", title="原动态已删除或不可见")
            if depth < 2 and item.get("orig")
            else None
        ),
    )
