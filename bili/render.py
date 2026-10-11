from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import CardConfig
from .models import Card


def qr_png(url: str) -> bytes:
    output = io.BytesIO()
    qrcode.make(url, box_size=6, border=2).save(output, format="PNG")
    return output.getvalue()


def display_time(card: Card) -> str:
    if not card.timestamp:
        return "哔哩哔哩"
    return datetime.fromtimestamp(card.timestamp, timezone(timedelta(hours=8))).strftime("%Y.%m.%d  %H:%M")


def card_html(card: Card, config: CardConfig, assets: dict[str, str]) -> str:
    def esc(value):
        return html.escape(str(value), quote=True)

    def image_tag(url: str, css: str = "") -> str:
        data = assets.get(url, "")
        return f'<img class="{css}" src="{data}">' if data else ""

    pics = "".join(image_tag(url) for url in card.images[: config.max_images])
    original = ""
    if card.original:
        src = card.original
        original = (
            f'<section class="original"><b>↗ {esc(src.author)}</b><h3>{esc(src.title)}</h3>'
            f"<p>{esc(src.text[:600])}</p>"
            f'<div class="images">{"".join(image_tag(u) for u in src.images[:3])}</div></section>'
        )
    qr = base64.b64encode(qr_png(card.url)).decode() if card.url else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">
<style>
*{{box-sizing:border-box}}body{{margin:0;width:{config.width}px;padding:28px;
background:linear-gradient(135deg,#fce9ef,#e9f6fc);color:#233044;
font-family:'Noto Sans CJK SC','Microsoft YaHei','PingFang SC',sans-serif}}
.card{{border-radius:24px;background:#fff;overflow:hidden;box-shadow:0 8px 32px #52728a18}}
.ribbon{{height:7px;background:{config.accent}}}.inner{{padding:30px}}
header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:26px}}
.brand{{font-size:27px;font-weight:800;color:{config.accent};letter-spacing:-1px}}
.tag{{border-radius:8px;background:#f5f6fa;padding:7px 13px;color:#627080;font-size:17px}}
.author{{display:flex;gap:14px;align-items:center;margin-bottom:26px}}
.avatar{{width:60px;height:60px;border-radius:50%;object-fit:cover}}
.name{{font-size:23px;font-weight:700}}.meta{{font-size:15px;color:#8a94a3;margin-top:6px}}
h1{{font-size:30px;line-height:1.45;overflow-wrap:anywhere;margin:0 0 18px}}
p{{font-size:21px;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 20px}}
.images{{display:grid;gap:10px;
grid-template-columns:repeat({min(3, max(1, len(card.images[: config.max_images])))},1fr)}}
.images img{{width:100%;height:210px;object-fit:cover;border-radius:12px}}
.images img:only-child{{height:auto;max-height:620px;object-fit:contain;background:#f5f6fa}}
.original{{background:#f5f7fa;padding:22px;border-radius:14px;margin-top:20px}}
.original h3{{font-size:21px}}.original p{{font-size:19px}}.original .images img{{max-height:260px}}
.stats{{font-size:17px;color:#7b8796;margin:24px 0}}
footer{{border-top:1px solid #eef0f4;padding-top:18px;display:flex;align-items:center;gap:18px}}
footer img{{width:82px;height:82px}}.link{{font-size:14px;color:#8590a0;overflow-wrap:anywhere}}
.foot-title{{font-size:16px;color:#536176;margin-bottom:7px}}
</style></head><body><article class="card"><div class="ribbon"></div><div class="inner">
<header><span class="brand">bilibili</span><span class="tag">{esc(card.kind)}</span></header>
<div class="author">{image_tag(card.avatar, "avatar")}<div><div class="name">{esc(card.author)}</div>
<div class="meta">{esc(display_time(card))}{(" · UID " + esc(card.uid)) if card.uid else ""}</div></div></div>
<h1>{esc(card.title[:200])}</h1><p>{esc(card.text[: config.max_text])}
{"…" if len(card.text) > config.max_text else ""}</p>
<div class="images">{pics}</div>{original}<div class="stats">{esc(" · ".join(card.stats))}</div>
<footer>{f'<img src="data:image/png;base64,{qr}">' if qr else ""}<div>
<div class="foot-title">扫码查看原内容</div><div class="link">{esc(card.url)}</div>
<div class="meta">MaiBot · Bilibili Dynamic</div></div></footer>
</div></article></body></html>"""


def find_font(configured: str) -> str:
    if configured:
        if not Path(configured).is_file():
            raise ValueError("配置的中文字体文件不存在")
        return configured
    paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for path in paths:
        if Path(path).is_file():
            return path
    raise ValueError("Pillow 渲染需要中文字体，请安装 Noto Sans CJK 或配置 cards.font_path")


def pillow_card(card: Card, config: CardConfig, assets: dict[str, str]) -> bytes:
    font_path = find_font(config.font_path)

    def font(size):
        return ImageFont.truetype(font_path, size)

    width = config.width
    canvas = Image.new("RGB", (width, 7000), "#f6edf3")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((24, 24, width - 24, 6975), radius=22, fill="white")
    draw.rounded_rectangle((24, 24, width - 24, 35), radius=5, fill=config.accent)
    left, right = 54, width - 54
    y = 58

    def text(value: str, size=23, color="#233044", max_lines=100, x=None, max_width=None):
        nonlocal y
        ft = font(size)
        x = left if x is None else x
        max_width = right - x if max_width is None else max_width
        line, lines = "", []
        for char in str(value):
            if char == "\n" or (line and draw.textlength(line + char, font=ft) > max_width):
                lines.append(line)
                line = "" if char == "\n" else char
            else:
                line += char
        if line:
            lines.append(line)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:-2] + "…"
        for line in lines:
            draw.text((x, y), line, font=ft, fill=color)
            y += int(size * 1.55)

    def decode(url: str):
        value = assets.get(url, "")
        if not value:
            return None
        with Image.open(io.BytesIO(base64.b64decode(value.split(",", 1)[-1]))) as im:
            return im.convert("RGB")

    def pictures(urls: list[str]):
        nonlocal y
        images = [im for url in urls if (im := decode(url)) is not None]
        if not images:
            return
        columns = min(3, len(images))
        cell = (right - left - 10 * (columns - 1)) // columns
        if len(images) == 1:
            im = images[0]
            im.thumbnail((right - left, 620))
            canvas.paste(im, (left + (right - left - im.width) // 2, y))
            y += im.height + 20
        else:
            for index, im in enumerate(images):
                tile = ImageOps.fit(im, (cell, cell))
                canvas.paste(tile, (left + index % columns * (cell + 10), y + index // columns * (cell + 10)))
            y += ((len(images) + columns - 1) // columns) * (cell + 10) + 10

    draw.text((left, y), "bilibili", font=font(30), fill=config.accent)
    label = card.kind
    draw.text((right - draw.textlength(label, font=font(20)), y + 8), label, font=font(20), fill="#728093")
    y += 72
    avatar = decode(card.avatar)
    author_x = left
    if avatar:
        avatar = ImageOps.fit(avatar, (58, 58))
        mask = Image.new("L", (58, 58))
        ImageDraw.Draw(mask).ellipse((0, 0, 58, 58), fill=255)
        canvas.paste(avatar, (left, y), mask)
        author_x += 74
    text(card.author, 24, max_lines=1, x=author_x)
    text(display_time(card), 16, "#8a94a3", max_lines=1, x=author_x)
    y += 24
    text(card.title[:200], 30, max_lines=4)
    y += 12
    text(card.text[: config.max_text], 22, max_lines=45)
    y += 20
    pictures(card.images[: config.max_images])
    if card.original:
        draw.line((left, y, right, y), fill="#e4e9ef", width=2)
        y += 22
        text(f"转发自 {card.original.author}", 20, config.accent, max_lines=1)
        text(card.original.searchable_text[:600], 20, "#536176", max_lines=14)
        y += 14
        pictures(card.original.images[:3])
    y += 12
    text(" · ".join(card.stats), 17, "#8290a0", max_lines=2)
    y += 20
    draw.line((left, y, right, y), fill="#e9edf2", width=1)
    y += 20
    bottom_y = y
    if card.url:
        qr = (
            Image.open(io.BytesIO(qr_png(card.url)))
            .convert("RGB")
            .resize((100, 100), Image.Resampling.NEAREST)
        )
        canvas.paste(qr, (left, y))
    text("扫码查看原内容", 18, "#536176", x=left + 118)
    text(card.url, 13, "#8290a0", max_lines=3, x=left + 118)
    text("MaiBot · Bilibili Dynamic", 14, "#8290a0", x=left + 118)
    height = max(y, bottom_y + 100) + 54
    canvas = canvas.crop((0, 0, width, height))
    ImageDraw.Draw(canvas).rectangle((0, height - 24, width, height), fill="#f6edf3")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


class Renderer:
    def __init__(self, config: CardConfig, client, ctx):
        self.config, self.client, self.ctx = config, client, ctx
        self.cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._limit = asyncio.Semaphore(2)
        self._host_unavailable_until = 0.0

    async def render(self, card: Card) -> str:
        if self.config.renderer == "text":
            return ""
        key = hashlib.sha256(card.to_json().encode()).hexdigest()
        if cached := self.cache.get(key):
            if cached[0] > time.monotonic():
                self.cache.move_to_end(key)
                return cached[1]
            del self.cache[key]
        async with self._limit:
            urls = [card.avatar, *card.images[: self.config.max_images]]
            if card.original:
                urls.extend(card.original.images[:3])
            urls = list(dict.fromkeys(u for u in urls if u))
            values = await asyncio.gather(*(self.client.image_data(u) for u in urls))
            assets = dict(zip(urls, values, strict=True))
            image = ""
            if self.config.renderer in {"auto", "host"} and time.monotonic() >= self._host_unavailable_until:
                try:
                    document = await asyncio.to_thread(card_html, card, self.config, assets)
                    result = await asyncio.wait_for(
                        self.ctx.render.html2png(
                            document,
                            selector="body",
                            viewport={"width": self.config.width, "height": 800},
                            device_scale_factor=1,
                            full_page=True,
                            allow_network=False,
                        ),
                        timeout=25,
                    )
                    image = result.get("image_base64", "")
                    if not image:
                        raise ValueError("Host 未返回图片")
                except Exception as exc:
                    self._host_unavailable_until = time.monotonic() + 300
                    self.ctx.logger.warning("Bilibili Host 渲染不可用，尝试本地渲染: %s", type(exc).__name__)
            if not image:
                try:
                    data = await asyncio.to_thread(pillow_card, card, self.config, assets)
                    image = base64.b64encode(data).decode()
                except Exception as exc:
                    self.ctx.logger.warning("Bilibili 卡片渲染不可用，改发文本: %s", type(exc).__name__)
                    return ""
            self.cache[key] = (time.monotonic() + self.config.cache_seconds, image)
            while len(self.cache) > 16:
                self.cache.popitem(last=False)
            return image
