import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest

from bili.client import BiliClient, BiliError
from bili.config import BiliConfig
from bili.models import Card
from bili.service import Monitor
from bili.store import Store
from tests.conftest import dynamic


def monitor(store, config=None):
    client = AsyncMock()
    client.logged_in = True
    client.live_card = BiliClient.live_card
    client.season_card = BiliClient.season_card
    return Monitor(
        store, client, config or BiliConfig(), AsyncMock(return_value=True), logging.getLogger("test")
    )


async def add(store, stream="a", uid="42", created=None, options=None):
    await store.add(stream, "user", uid, "测试", created_at=created or time.time() - 100, options=options)
    return (await store.subscriptions(stream))[-1]


async def test_feed_pins_dedup_and_per_chat_filters(store):
    now = int(time.time())
    await add(store)
    await add(store, "b", options={"types": ["视频"], "type_mode": "white"})
    mon = monitor(store)
    mon.client.feed.return_value = {
        "items": [dynamic("old-pin", ts=1), dynamic("new", ts=now)],
        "has_more": False,
    }
    await mon.poll_feed()
    await mon.poll_feed()
    assert await store.pending_count("a") == 1
    assert await store.pending_count("b") == 0
    await mon.dispatch()
    assert mon.send_card.await_count == 1
    await mon.poll_feed()
    assert await store.pending_count() == 0


async def test_pagination_continues_next_cycle_without_skipping(store):
    now = int(time.time())
    await add(store, created=now - 50)
    config = BiliConfig()
    config.polling.max_pages = 1
    mon = monitor(store, config)
    mon.client.feed.side_effect = [
        {"items": [dynamic("1", ts=now)], "has_more": True, "offset": "page2"},
        {"items": [dynamic("2", ts=now - 10)], "has_more": True, "offset": "page3"},
        {"items": [dynamic("3", ts=now - 20)], "has_more": False},
    ]
    for _ in range(3):
        await mon.poll_feed()
    assert [call.args[0] for call in mon.client.feed.await_args_list] == ["", "page2", "page3"]
    assert await store.pending_count() == 3
    state = await store.get_state("cursor:feed")
    assert state["offset"] == "" and state["watermark"] == now


async def test_send_failure_persists_through_restart_and_other_recipient_succeeds(store, monkeypatch):
    a, b = await add(store), await add(store, "b")
    card = Card("event", "动态", "测试")
    await store.record([(a, "event", card), (b, "event", card)])
    mon = monitor(store)
    mon.send_card.side_effect = [False, True]
    await mon.dispatch()
    assert await store.pending_count("a") == 1
    assert await store.pending_count("b") == 0
    # Reopen the actual database rather than reusing in-memory state.
    reopened = Store(store.path)
    await reopened.open()
    try:
        later = time.time() + 100
        monkeypatch.setattr("bili.store.time.time", lambda: later)
        second = monitor(reopened)
        await second.dispatch()
        assert second.send_card.await_count == 1
        assert await reopened.pending_count() == 0
        assert await reopened.record([(a, "event", card)]) == 0
    finally:
        await reopened.close()


async def test_unsubscribe_cascades_queue_and_isolates_chats(store):
    a, b = await add(store), await add(store, "b")
    card = Card("1", "动态", "测试")
    await store.record([(a, "e1", card), (b, "e1", card)])
    await store.remove("a", "42")
    mon = monitor(store)
    await mon.dispatch()
    assert mon.send_card.await_args.args[0] == "b"
    assert mon.send_card.await_count == 1
    # An in-flight poll cannot recreate a removed subscription's delivery.
    assert await store.record([(a, "e2", card)]) == 0


async def test_duplicate_add_limit_and_options_race(store):
    sub = await add(store)
    assert not await store.add("a", "user", "42", "other")
    with pytest.raises(ValueError):
        await store.add("a", "user", "43", "other", limit=1)
    await store.update_options("a", "42", {"paused": True})
    assert await store.record([(sub, "new", Card("1", "动态", "测试"))]) == 0


async def test_pause_does_not_send_pending_and_resume_can_retry(store, monkeypatch):
    sub = await add(store)
    await store.record([(sub, "e1", Card("1", "动态", "测试"))])
    await store.update_options("a", "42", {"paused": True})
    mon = monitor(store)
    await mon.dispatch()
    mon.send_card.assert_not_awaited()
    assert await store.pending_count() == 1
    await store.update_options("a", "42", {"paused": False})
    later = time.time() + 100
    monkeypatch.setattr("bili.store.time.time", lambda: later)
    await mon.dispatch()
    assert await store.pending_count() == 0


async def test_space_error_rotates_to_other_uid(store):
    await add(store, uid="42")
    await add(store, uid="43")
    mon = monitor(store)
    mon.client.logged_in = False
    mon.client.feed.side_effect = [BiliError("网络失败"), {"items": [], "has_more": False}]
    with pytest.raises(BiliError):
        await mon.poll_space()
    await mon.poll_space()
    assert [c.args[1] for c in mon.client.feed.await_args_list] == ["42", "43"]
    await mon.poll_feed()
    assert mon.client.feed.await_count == 2


async def test_live_baseline_start_end_and_restart(store):
    await add(store)
    mon = monitor(store)
    now = int(time.time())

    def room(status, start=0):
        return {
            "42": {
                "uid": 42,
                "room_id": 100,
                "uname": "主播",
                "title": "标题",
                "live_status": status,
                "live_time": start,
            }
        }

    mon.client.live_status.return_value = room(0)
    await mon.poll_live()
    assert await store.pending_count() == 0
    mon.client.live_status.return_value = room(1, now)
    await mon.poll_live()
    await mon.poll_live()
    assert await store.pending_count() == 1
    new_mon = monitor(store)
    new_mon.client.live_status.return_value = room(1, now)
    await new_mon.poll_live()
    assert await store.pending_count() == 1
    new_mon.client.live_status.return_value = room(2)
    await new_mon.poll_live()
    await new_mon.poll_live()
    assert await store.pending_count() == 2


async def test_new_subscriber_does_not_receive_existing_live_transition(store):
    await add(store, created=time.time() - 100)
    mon = monitor(store)
    await store.record(
        [], state={"live:42": {"status": 0, "start": 0, "observed": time.time() - 90, "data": {}}}
    )
    await add(store, "b", created=time.time())
    mon.client.live_status.return_value = {
        "42": {"uid": 42, "room_id": 100, "live_status": 1, "live_time": 50}
    }
    await mon.poll_live()
    assert await store.pending_count("a") == 1
    assert await store.pending_count("b") == 0


async def test_live_baseline_keeps_subsecond_subscription_time(store, monkeypatch):
    now = int(time.time())
    await add(store, created=now + 0.1)
    monkeypatch.setattr("bili.service.time.time", lambda: now + 0.2)
    mon = monitor(store)
    mon.client.live_status.return_value = {"42": {"uid": 42, "room_id": 100, "live_status": 0}}
    await mon.poll_live()
    monkeypatch.setattr("bili.service.time.time", lambda: now + 30)
    mon.client.live_status.return_value = {
        "42": {"uid": 42, "room_id": 100, "live_status": 1, "live_time": now + 10}
    }
    await mon.poll_live()
    assert await store.pending_count("a") == 1


async def test_bangumi_baseline_and_new_episode(store):
    await store.add("a", "season", "ss1", "番剧")
    mon = monitor(store)
    first = {"id": 10, "aid": 10, "cid": 10, "title": "第一话"}
    second = {"id": 11, "aid": 11, "cid": 11, "title": "第二话"}
    preview = {"id": 12, "title": "第三话（未播）"}
    mon.client.season.return_value = {"season_id": 1, "title": "番剧", "episodes": [first]}
    await mon.poll_seasons()
    assert await store.pending_count() == 0
    await store.add("b", "season", "ss1", "番剧", options={"episode_baseline": ["10", "11"]})
    mon.client.season.return_value["episodes"] = [first, second, preview]
    await mon.poll_seasons()
    await mon.poll_seasons()
    assert await store.pending_count("a") == 1
    assert await store.pending_count("b") == 0


async def test_allowlist_applies_to_polling_and_queued_delivery(store):
    sub = await add(store)
    await store.record([(sub, "e1", Card("1", "动态", "测试"))])
    config = BiliConfig()
    config.access.allowed_streams = ["b"]
    mon = monitor(store, config)
    await mon.poll_feed()
    await mon.dispatch()
    mon.client.feed.assert_not_awaited()
    mon.send_card.assert_not_awaited()


async def test_start_stop_cancels_background_tasks(store):
    mon = monitor(store)
    mon.start()
    tasks = list(mon.tasks)
    await asyncio.sleep(0)
    await mon.stop()
    assert all(task.done() for task in tasks)
    assert mon.tasks == []
