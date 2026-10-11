from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from .client import BiliClient, BiliError
from .config import BiliConfig
from .models import Card, Subscription, dynamic_card, timestamp
from .store import Store


class Monitor:
    def __init__(
        self,
        store: Store,
        client: BiliClient,
        config: BiliConfig,
        send_card: Callable[[str, Card], Awaitable[bool]],
        logger: logging.Logger,
    ):
        self.store, self.client, self.config = store, client, config
        self.send_card, self.logger = send_card, logger
        self.tasks: list[asyncio.Task] = []
        self._space_due: dict[str, float] = {}
        self.health: dict[str, dict] = {}

    def start(self) -> None:
        cfg = self.config.polling
        jobs = [
            ("feed", self.poll_feed, cfg.feed_interval),
            ("space", self.poll_space, cfg.space_tick),
            ("live", self.poll_live, cfg.live_interval),
            ("season", self.poll_seasons, cfg.season_interval),
            ("send", self.dispatch, 1),
            ("cleanup", self.store.prune, 3600),
        ]
        self.tasks = [
            asyncio.create_task(self._loop(name, job, interval), name=f"bili:{name}")
            for name, job, interval in jobs
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _loop(self, name: str, job, interval: float) -> None:
        failures = 0
        while True:
            started = time.monotonic()
            try:
                await job()
                failures = 0
                self.health[name] = {"ok": True, "time": int(time.time())}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                detail = str(exc) if isinstance(exc, BiliError) else type(exc).__name__
                self.health[name] = {"ok": False, "error": detail, "time": int(time.time())}
                self.logger.warning("Bilibili %s: %s", name, detail)
            delay = max(interval, min(900, interval * 2 ** min(failures, 6)))
            await asyncio.sleep(max(0.5, delay - (time.monotonic() - started)))

    async def active_subscriptions(self) -> list[Subscription]:
        allowed = self.config.access.allowed_streams
        return [
            s
            for s in await self.store.subscriptions()
            if not s.options.get("paused") and (not allowed or s.stream_id in allowed)
        ]

    async def _scan(self, subscriptions: list[Subscription], uid: str = "") -> None:
        if not subscriptions:
            return
        key = f"cursor:{uid or 'feed'}"
        lower = max(
            min(s.created_at for s in subscriptions), time.time() - self.config.polling.catchup_hours * 3600
        )
        cursor = await self.store.get_state(key, {})
        watermark = max(lower, cursor.get("watermark", lower))
        offset = cursor.get("offset", "")
        high = cursor.get("high", watermark)
        by_uid: dict[str, list[Subscription]] = {}
        for sub in subscriptions:
            by_uid.setdefault(sub.subject, []).append(sub)
        for _ in range(self.config.polling.max_pages):
            page = await self.client.feed(offset, uid)
            items = page.get("items") or []
            events = []
            times = []
            for item in items:
                card = dynamic_card(item)
                times.append(card.timestamp)
                high = max(high, card.timestamp)
                if card.kind in {"直播", "直播结束"}:
                    continue  # Actual live transitions are handled by the batch live endpoint.
                if card.timestamp < watermark - 2:
                    continue
                for sub in by_uid.get(card.uid, []):
                    if card.timestamp >= sub.created_at and sub.accepts(card):
                        events.append((sub, f"dynamic:{card.id}", card))
            new_offset = str(page.get("offset") or "")
            # A pinned old dynamic must not prematurely end a page containing recent posts.
            exhausted = (
                not items
                or not page.get("has_more")
                or not new_offset
                or new_offset == offset
                or (times and max(times) < watermark - 2)
            )
            state = {
                "watermark": max(watermark, high) if exhausted else watermark,
                "offset": "" if exhausted else new_offset,
                "high": high,
            }
            await self.store.record(events, state={key: state})
            if exhausted:
                return
            offset = new_offset
        self.logger.debug("Bilibili %s 分页达到单轮上限，下轮继续补查", uid or "feed")

    async def poll_feed(self) -> None:
        if self.config.polling.mode == "space" or not self.client.logged_in:
            return
        subs = [
            s
            for s in await self.active_subscriptions()
            if s.source == "user" and s.options.get("dynamic", True)
        ]
        await self._scan(subs)

    async def poll_space(self) -> None:
        if self.config.polling.mode == "feed":
            return
        subs = [
            s
            for s in await self.active_subscriptions()
            if s.source == "user" and s.options.get("dynamic", True)
        ]
        subjects = {s.subject for s in subs}
        self._space_due = {uid: due for uid, due in self._space_due.items() if uid in subjects}
        if not subjects:
            return
        uid = min(subjects, key=lambda uid: (self._space_due.get(uid, 0), uid))
        if self._space_due.get(uid, 0) > time.monotonic():
            return
        cfg = self.config.polling
        interval = cfg.space_interval if self.client.logged_in else cfg.anonymous_space_interval
        # Rotate even when a particular UP cannot be queried; it must not starve the others.
        self._space_due[uid] = time.monotonic() + interval
        await self._scan([s for s in subs if s.subject == uid], uid)

    async def poll_live(self) -> None:
        subs = [
            s for s in await self.active_subscriptions() if s.source == "user" and s.options.get("live", True)
        ]
        uids = sorted({s.subject for s in subs})
        for start in range(0, len(uids), 100):
            result = await self.client.live_status(uids[start : start + 100])
            for uid, data in result.items():
                uid = str(uid)
                if uid not in uids:
                    continue
                key = f"live:{uid}"
                previous = await self.store.get_state(key)
                current = {
                    "status": int(data.get("live_status", 0)),
                    "start": timestamp(data.get("live_time")),
                    "observed": time.time(),
                    "data": data,
                }
                events = []
                if previous is not None:
                    started = current["status"] == 1 and (
                        previous["status"] != 1
                        or (current["start"] and previous["start"] and current["start"] != previous["start"])
                    )
                    ended = previous["status"] == 1 and current["status"] != 1
                    if started or ended:
                        card = self.client.live_card(previous["data"] if ended else data, ended=ended)
                        event_time = (
                            previous["start"] or previous["observed"]
                            if ended
                            else current["start"] or current["observed"]
                        )
                        event_key = f"live:{uid}:{event_time}:{'end' if ended else 'start'}"
                        events = [
                            (s, event_key, card)
                            for s in subs
                            if s.subject == uid and s.created_at <= previous["observed"] and s.accepts(card)
                        ]
                # No historical start notification on first observation, including after initial subscription.
                await self.store.record(events, state={key: current})

    async def poll_seasons(self) -> None:
        subs = [s for s in await self.active_subscriptions() if s.source == "season"]
        errors = []
        for subject in sorted({s.subject for s in subs}):
            try:
                data = await self.client.season(subject)
                episodes = data.get("episodes") or []
                # Unpublished preview entries can have an ID; only an actual playable episode counts.
                episodes = [ep for ep in episodes if ep.get("id") and ep.get("aid") and ep.get("cid")]
                ids = {str(ep["id"]) for ep in episodes}
                key = f"season:{subject}"
                previous = await self.store.get_state(key)
                events = []
                if previous is not None:
                    for ep in episodes:
                        if str(ep["id"]) in previous:
                            continue
                        card = self.client.season_card(data, ep)
                        for sub in subs:
                            if (
                                sub.subject == subject
                                and sub.accepts(card)
                                and str(ep["id"]) not in sub.options.get("episode_baseline", [])
                            ):
                                events.append((sub, f"episode:{ep['id']}", card))
                # Retain IDs that temporarily disappear, avoiding repeated episode notifications.
                await self.store.record(events, state={key: sorted(ids | set(previous or []))})
            except BiliError as exc:
                self.logger.warning("Bilibili 番剧 %s: %s", subject, exc)
                errors.append(subject)
        if errors:
            raise BiliError(f"{len(errors)} 个番剧查询失败，将在退避后重试")

    async def dispatch(self) -> None:
        for delivery in await self.store.due():
            sub = await self.store.current_subscription(delivery["subscription_id"])
            if sub is None:
                continue
            allowed = self.config.access.allowed_streams
            if sub.options.get("paused") or (allowed and sub.stream_id not in allowed):
                await self.store.failed(delivery["id"], delivery["attempts"])
                continue
            card = Card.from_dict(json.loads(delivery["card"]))
            if not sub.accepts(card):
                await self.store.delivered(delivery["id"])
                continue
            try:
                ok = await self.send_card(sub.stream_id, card)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("Bilibili 推送失败，稍后重试: %s", type(exc).__name__)
                ok = False
            if ok:
                await self.store.delivered(delivery["id"])
            else:
                await self.store.failed(delivery["id"], delivery["attempts"])
