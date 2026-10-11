import pytest

from bili.links import Link, allowed_url, extract_links, message_text, parse_url
from bili.models import Card, Subscription, dynamic_card
from tests.conftest import dynamic


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bilibili.com/video/BV1xx411c7mD?p=2", Link("video", "BV1xx411c7mD")),
        ("https://t.bilibili.com/1234", Link("dynamic", "1234")),
        ("https://m.bilibili.com/dynamic/1234", Link("dynamic", "1234")),
        ("https://www.bilibili.com/opus/1234", Link("dynamic", "1234")),
        ("https://live.bilibili.com/h5/1234", Link("live", "1234")),
        ("https://space.bilibili.com/42/dynamic", Link("user", "42")),
        ("www.bilibili.com/read/cv1234", Link("article", "1234")),
        ("https://www.bilibili.com/bangumi/play/ep1234", Link("season", "ep1234")),
        ("https://b23.tv/abc123", Link("short", "https://b23.tv/abc123")),
        ("https://www.bilibili.com.evil.test/video/BV1xx411c7mD", None),
        ("https://www.bilibili.com@evil.test/video/BV1xx411c7mD", None),
        ("https://www.bilibili.com:9000/video/BV1xx411c7mD", None),
    ],
)
def test_url_types_and_host_validation(url, expected):
    assert parse_url(url) == expected


def test_json_share_card_and_dedup():
    message = {
        "raw_message": [
            {"type": "json", "data": r'{"meta":{"detail_1":{"qqdocurl":"https:\/\/b23.tv\/ABc123"}}}'},
            {"type": "text", "data": "BV1xx411c7mD https://www.bilibili.com/video/BV1xx411c7mD"},
            {"type": "image", "data": "BV1111111111"},
        ]
    }
    assert extract_links(message_text(message)) == [
        Link("short", "https://b23.tv/ABc123"),
        Link("video", "BV1xx411c7mD"),
    ]
    assert extract_links("看 BV1xx411c7mD", bare_ids=False) == []
    assert extract_links("av42 cv12 ss10 ep11", limit=2) == [Link("video", "av42"), Link("article", "12")]
    assert not allowed_url("file:///etc/passwd", {""})


def test_modern_opus_video_and_deleted_forward():
    item = dynamic(kind="DYNAMIC_TYPE_DRAW")
    item["modules"]["module_dynamic"]["major"] = {
        "opus": {
            "title": "标题",
            "summary": {"text": "正文"},
            "pics": [{"url": "https://i0.hdslb.com/a.jpg"}],
        }
    }
    card = dynamic_card(item)
    assert (card.kind, card.title, card.text, len(card.images)) == ("图文", "标题", "正文", 1)
    forward = dynamic(kind="DYNAMIC_TYPE_FORWARD")
    forward["orig"] = {"type": "DYNAMIC_TYPE_NONE"}
    assert "不可见" in dynamic_card(forward).original.title
    video = dynamic(kind="DYNAMIC_TYPE_AV")
    video["modules"]["module_dynamic"]["major"] = {"archive": {"title": "视频标题", "cover": "pic"}}
    assert dynamic_card(video).title == "视频标题"
    restored = Card.from_dict(__import__("json").loads(dynamic_card(forward).to_json()))
    assert restored.original.id == "deleted"


def test_filter_modes_and_original_text():
    sub = Subscription(1, "group", "user", "42", "name", 0)
    card = Card("1", "转发", "转发标题", original=Card("2", "视频", "抽奖活动"))
    assert sub.accepts(card)
    sub.options = {"types": ["转发"], "type_mode": "black"}
    assert not sub.accepts(card)
    sub.options = {"types": ["视频"], "type_mode": "white"}
    assert not sub.accepts(card)
    sub.options = {"regex": "抽奖", "regex_mode": "black"}
    assert not sub.accepts(card)
    sub.options["regex_mode"] = "white"
    assert sub.accepts(card)
    sub.options = {"regex": "[invalid"}
    assert not sub.accepts(card)
    sub.options = {"paused": True}
    assert not sub.accepts(card)


def test_pathological_regex_has_time_limit():
    sub = Subscription(1, "group", "user", "42", "name", 0, {"regex": "(a+)+$"})
    assert not sub.accepts(Card("1", "动态", "a" * 11000 + "!"))
