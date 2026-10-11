import base64
import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from bili.config import CardConfig
from bili.models import Card
from bili.render import Renderer, card_html, find_font, pillow_card


def test_html_escapes_untrusted_content_and_embeds_only_fetched_assets():
    card = Card(
        "1",
        "动态",
        "<script>alert(1)</script>",
        text='<img src="http://localhost">',
        images=["http://127.0.0.1:80/secret"],
        url="https://t.bilibili.com/1",
    )
    html = card_html(card, CardConfig(), {})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'src="http://127.0.0.1' not in html
    assert 'src="http://localhost' not in html


def test_pillow_unicode_multi_image_and_long_text_have_bounded_dimensions():
    try:
        find_font("")
    except ValueError:
        pytest.skip("Install a CJK font to run visual renderer tests")
    output = io.BytesIO()
    Image.new("RGB", (200, 300), "skyblue").save(output, format="PNG")
    image = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
    card = Card(
        "1",
        "图文",
        "中文标题 🌸 test",
        text="内容换行\n" * 300,
        images=["a"] * 9,
        original=Card("2", "视频", "原内容", text="测试\n" * 100, images=["a"] * 3),
        url="https://t.bilibili.com/1",
    )
    png = pillow_card(card, CardConfig(), {"a": image})
    with Image.open(io.BytesIO(png)) as decoded:
        assert decoded.width == 880
        assert 1000 < decoded.height < 7000


async def test_render_failure_falls_back_to_text_and_host_is_cooled_down():
    cfg = CardConfig(font_path="/nonexistent/font.ttf")
    ctx = SimpleNamespace(
        render=SimpleNamespace(html2png=AsyncMock(side_effect=RuntimeError("offline"))),
        logger=logging.getLogger("test"),
    )
    renderer = Renderer(cfg, SimpleNamespace(image_data=AsyncMock(return_value="")), ctx)
    card = Card("1", "动态", "测试")
    assert await renderer.render(card) == ""
    assert await renderer.render(card) == ""
    assert ctx.render.html2png.await_count == 1
