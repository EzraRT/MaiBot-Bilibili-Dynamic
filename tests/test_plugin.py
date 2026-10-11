import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from maibot_sdk.context import PluginContext, PluginPaths


@pytest.fixture
async def plugin(plugin_module, tmp_path, monkeypatch):
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)
    calls = []

    async def rpc(method, plugin_id, params, **kwargs):
        calls.append((method, params))
        return True

    instance = plugin_module.create_plugin()
    config = instance.build_default_config()
    config["access"]["admins"] = ["qq:owner"]
    config["cards"]["renderer"] = "text"
    instance.set_plugin_config(config)
    instance._set_context(
        PluginContext(
            "com.ezrart.bilibili-dynamic",
            rpc_call=rpc,
            paths=PluginPaths(tmp_path / "data", tmp_path / "temp"),
        )
    )
    await instance.on_load()
    await instance.monitor.stop()
    instance.test_calls = calls
    yield instance
    await instance.on_unload()


def command_args(text, user="owner", stream="group-a", group="100"):
    return {
        "stream_id": stream,
        "platform": "qq",
        "user_id": user,
        "group_id": group,
        "matched_groups": {"args": text},
        "message": {
            "platform": "qq",
            "session_id": stream,
            "message_info": {
                "user_info": {"user_id": user},
                "group_info": {"group_id": group} if group else None,
            },
        },
    }


def sent_text(plugin):
    return "\n".join(str(params) for _, params in plugin.test_calls)


async def test_real_sdk_loader_and_component_registration(plugin):
    components = plugin.get_components()
    assert {c["name"] for c in components} == {"bili", "bili_links"}
    assert plugin.config.plugin.config_version == "1.0.0"
    schema = plugin.build_config_schema()
    assert schema


async def test_command_permissions_and_chat_isolation(plugin):
    plugin.client.user = AsyncMock(return_value={"mid": 42, "name": "UP"})
    result = await plugin.handle_command(**command_args("add 42", user="visitor"))
    assert result[0] is False
    plugin.client.user.assert_not_awaited()
    assert await plugin.store.subscriptions() == []
    result = await plugin.handle_command(**command_args("add 42"))
    assert result[0] is True
    assert len(await plugin.store.subscriptions("group-a")) == 1
    result = await plugin.handle_command(**command_args("del 42", stream="group-b"))
    assert result[0] is False
    assert len(await plugin.store.subscriptions("group-a")) == 1
    await plugin.handle_command(**command_args('filter 42 regex black "抽奖|广告"'))
    assert (await plugin.store.subscriptions("group-a"))[0].options["regex"] == "抽奖|广告"


async def test_public_subscription_does_not_grant_login_permission(plugin):
    plugin.settings.access.allow_public_subscribe = True
    plugin.client.qr_generate = AsyncMock()
    result = await plugin.handle_command(**command_args("login", user="visitor", group=""))
    assert not result[0]
    result = await plugin.handle_command(**command_args("login", group="100"))
    assert not result[0]
    plugin.client.qr_generate.assert_not_awaited()


async def test_link_event_sdk_segments_background_and_dedup(plugin):
    from bili.models import Card

    plugin.client.resolve = AsyncMock(
        return_value=Card("BV1xx411c7mD", "视频", "测试", url="https://example.invalid")
    )
    message = {"session_id": "group-a", "raw_message": [{"type": "text", "data": "BV1xx411c7mD"}]}
    await plugin.handle_message(message=message)
    assert plugin._link_tasks
    await asyncio.gather(*plugin._link_tasks)
    before = len(plugin.test_calls)
    await plugin.handle_message(message=message)
    await asyncio.gather(*plugin._link_tasks)
    assert len(plugin.test_calls) == before
    assert plugin.client.resolve.await_count == 1
    await plugin.handle_message(message={**message, "session_id": "group-b"})
    await asyncio.gather(*plugin._link_tasks)
    assert len(plugin.test_calls) > before


async def test_config_reload_replaces_tasks_and_preserves_subscriptions(plugin):
    await plugin.store.add("group-a", "user", "42", "UP")
    old_client = plugin.client
    config = plugin.get_plugin_config_data()
    config["plugin"]["enabled"] = False
    await plugin.on_config_update("self", config, "1.0.0")
    assert old_client.http.is_closed
    assert plugin.monitor.tasks == []
    assert len(await plugin.store.subscriptions()) == 1
    assert not plugin.settings.plugin.enabled


async def test_reload_cancels_slow_link_and_command_without_deadlock(plugin):
    started = asyncio.Event()

    async def slow_request(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    plugin.client.resolve = AsyncMock(side_effect=slow_request)
    await plugin.handle_message(message={"session_id": "group-a", "raw_message": "BV1xx411c7mD"})
    await asyncio.wait_for(started.wait(), timeout=1)
    command = asyncio.create_task(plugin.handle_command(**command_args("list")))
    await asyncio.sleep(0)
    config = plugin.get_plugin_config_data()
    config["plugin"]["enabled"] = False
    await asyncio.wait_for(plugin.on_config_update("self", config, "1.0.0"), timeout=1)
    assert not plugin._link_tasks and not plugin._command_tasks
    assert command.done()


async def test_login_cookie_is_private_and_logout_clears_it(plugin):
    await asyncio.to_thread(plugin._save_cookie, "SESSDATA=private; bili_jct=csrf")
    path = Path(plugin.ctx.paths.data_dir) / "account.cookie"
    assert path.read_text() == "SESSDATA=private; bili_jct=csrf"
    if __import__("os").name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    plugin.client.cookie = path.read_text()
    await plugin.handle_command(**command_args("logout", group=""))
    assert not path.exists() and not plugin.client.logged_in
    assert "private" not in sent_text(plugin)


async def test_bad_input_returns_failure_and_does_not_change_state(plugin):
    for text in ["add nope", 'filter 42 regex black "["', "new 42 xyz", "clear", "badcommand"]:
        result = await plugin.handle_command(**command_args(text))
        assert not result[0]
    assert await plugin.store.subscriptions() == []


async def test_renderer_uses_real_sdk_render_and_hybrid_fields(plugin, plugin_module):
    from bili.models import Card

    captured = []

    async def rpc(method, plugin_id, params, **kwargs):
        captured.append((method, params))
        # Capability routing is still performed by the installed SDK.
        if "render" in str(params):
            return {"image_base64": base64.b64encode(b"png").decode()}
        return True

    plugin.ctx._rpc_call = rpc
    plugin.settings.cards.renderer = "host"
    plugin.renderer = plugin_module.Renderer(plugin.settings.cards, plugin.client, plugin.ctx)
    assert await plugin._send_card("group-a", Card("1", "动态", "文本", url="https://t.bilibili.com/1"))
    serialized = json.dumps(captured)
    assert "allow_network" in serialized and "send.hybrid" in serialized
    assert "segments" in serialized and "image" in serialized
