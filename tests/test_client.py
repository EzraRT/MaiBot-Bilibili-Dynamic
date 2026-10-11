import io
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from bili.client import BiliClient, BiliError, RiskControl
from bili.config import PollingConfig
from bili.links import Link


def client_for(handler, cookie=""):
    client = BiliClient(PollingConfig(), cookie, transport=httpx.MockTransport(handler))
    client._throttle = AsyncMock()
    return client


async def test_cookie_only_sent_to_api_and_shortlink_resolves():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "b23.tv":
            return httpx.Response(302, headers={"location": "https://www.bilibili.com/video/BV1xx411c7mD"})
        return httpx.Response(200, json={"code": 0, "data": {"bvid": "BV1xx411c7mD", "title": "标题"}})

    client = client_for(handler, "SESSDATA=secret; bili_jct=csrf")
    try:
        card = await client.resolve(Link("short", "https://b23.tv/ABC"))
        assert card.kind == "视频"
        assert "cookie" not in requests[0].headers
        assert requests[1].headers["cookie"] == "SESSDATA=secret; bili_jct=csrf"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "file:///etc/passwd",
        "https://b23.tv.evil.test/abc",
        "https://www.bilibili.com@evil.test/path",
    ],
)
async def test_short_redirect_rejects_non_bilibili(location):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"location": location})

    client = client_for(handler)
    try:
        with pytest.raises(BiliError):
            await client.resolve_short("https://b23.tv/abc")
        assert count == 1
    finally:
        await client.close()


@pytest.mark.parametrize(("status", "code"), [(412, 0), (429, 0), (200, -352)])
async def test_risk_control_stops_requests(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"code": code})

    client = BiliClient(PollingConfig(), transport=httpx.MockTransport(handler))
    try:
        for _ in range(2):
            with pytest.raises(RiskControl):
                await client.feed()
        assert len(calls) == 1
    finally:
        await client.close()


async def test_errors_do_not_disclose_server_message_or_cookie():
    client = client_for(
        lambda request: httpx.Response(200, json={"code": -101, "message": "secret"}), "SESSDATA=secret"
    )
    try:
        with pytest.raises(BiliError) as error:
            await client.feed()
        assert "secret" not in str(error.value)
    finally:
        await client.close()


async def test_wbi_signing_accepts_anonymous_nav():
    raw = "0123456789abcdef" * 4
    payload = {
        "code": -101,
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/{raw[:32]}.png",
                "sub_url": f"https://i0.hdslb.com/{raw[32:]}.png",
            }
        },
    }
    client = client_for(lambda request: httpx.Response(200, json=payload))
    try:
        params = await client.signed({"mid": "42", "text": "a!'()*b"})
        assert len(params["w_rid"]) == 32
        assert params["text"] == "ab"
        assert "wts" in params
    finally:
        await client.close()


async def test_assets_no_credentials_and_no_untrusted_fetch():
    output = io.BytesIO()
    Image.new("RGB", (100, 80), "pink").save(output, format="PNG")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=output.getvalue())

    client = client_for(handler, "SESSDATA=secret")
    try:
        assert await client.image_data("http://localhost/a.png") == ""
        assert await client.image_data("https://hdslb.com.evil.test/a.png") == ""
        result = await client.image_data("https://i0.hdslb.com/a.png")
        assert result.startswith("data:image/jpeg;base64,")
        assert "cookie" not in calls[0].headers
        assert len(calls) == 1
        assert await client.image_data("https://i0.hdslb.com/a.png") == result
        assert len(calls) == 1
    finally:
        await client.close()


async def test_qr_cookie_from_poll_url():
    client = client_for(
        lambda req: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "code": 0,
                    "url": "https://www.bilibili.com?SESSDATA=abc&bili_jct=def&DedeUserID=42",
                },
            },
        )
    )
    try:
        code, cookie = await client.qr_poll("qrcode-key")
        assert code == 0 and "SESSDATA=abc" in cookie
        assert "qrcode-key" not in cookie
    finally:
        await client.close()
