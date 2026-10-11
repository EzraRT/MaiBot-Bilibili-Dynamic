from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from .models import Card, Subscription


class Store:
    """Serialized SQLite transactions; no synchronous disk I/O on the Runner loop."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self.db: sqlite3.Connection | None = None

    async def _run(self, operation):
        async with self._lock:
            # A cancelled coroutine must not release the lock while its worker still uses the connection.
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def open(self) -> None:
        def operation():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    options TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(stream_id, source, subject)
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    event_key TEXT NOT NULL,
                    card TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry REAL NOT NULL DEFAULT 0,
                    UNIQUE(subscription_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_retry);
                PRAGMA user_version=1;
            """)
            self.db.commit()

        await self._run(operation)

    async def close(self) -> None:
        if self.db is not None:
            await self._run(self.db.close)
            self.db = None

    @staticmethod
    def _subscription(row) -> Subscription:
        data = dict(row)
        data["options"] = json.loads(data["options"])
        return Subscription(**data)

    async def subscriptions(self, stream_id: str = "") -> list[Subscription]:
        def operation():
            sql = "SELECT * FROM subscriptions"
            rows = (
                self.db.execute(sql + " WHERE stream_id=? ORDER BY id", (stream_id,))
                if stream_id
                else self.db.execute(sql + " ORDER BY id")
            )
            return [self._subscription(row) for row in rows]

        return await self._run(operation)

    async def add(
        self,
        stream_id: str,
        source: str,
        subject: str,
        name: str,
        *,
        limit=100,
        options: dict | None = None,
        created_at: float | None = None,
    ) -> bool:
        def operation():
            with self.db:
                if self.db.execute(
                    "SELECT 1 FROM subscriptions WHERE stream_id=? AND source=? AND subject=?",
                    (stream_id, source, subject),
                ).fetchone():
                    return False
                count = self.db.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE stream_id=?", (stream_id,)
                ).fetchone()[0]
                if count >= limit:
                    raise ValueError(f"当前会话最多允许 {limit} 个订阅")
                self.db.execute(
                    "INSERT INTO subscriptions(stream_id,source,subject,name,created_at,options) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        stream_id,
                        source,
                        subject,
                        name,
                        time.time() if created_at is None else created_at,
                        json.dumps(options or {}),
                    ),
                )
                return True

        return await self._run(operation)

    async def remove(self, stream_id: str, subject: str) -> int:
        def operation():
            with self.db:
                return self.db.execute(
                    "DELETE FROM subscriptions WHERE stream_id=? AND subject=?", (stream_id, subject)
                ).rowcount

        return await self._run(operation)

    async def clear(self, stream_id: str) -> int:
        def operation():
            with self.db:
                return self.db.execute("DELETE FROM subscriptions WHERE stream_id=?", (stream_id,)).rowcount

        return await self._run(operation)

    async def update_options(self, stream_id: str, subject: str, changes: dict) -> bool:
        def operation():
            with self.db:
                row = self.db.execute(
                    "SELECT id,options FROM subscriptions WHERE stream_id=? AND subject=?",
                    (stream_id, subject),
                ).fetchone()
                if not row:
                    return False
                options = {**json.loads(row["options"]), **changes}
                self.db.execute(
                    "UPDATE subscriptions SET options=? WHERE id=?", (json.dumps(options), row["id"])
                )
                return True

        return await self._run(operation)

    async def get_state(self, key: str, default=None):
        def operation():
            row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

        return await self._run(operation)

    async def record(self, events: list[tuple[Subscription, str, Card]], *, state: dict | None = None) -> int:
        """Persist event deliveries and observation cursors in one atomic commit."""

        def operation():
            inserted = 0
            with self.db:
                for sub, key, card in events:
                    # Re-read options: the subscription may have changed while the API request was in flight.
                    row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (sub.id,)).fetchone()
                    if row is None or not self._subscription(row).accepts(card):
                        continue
                    cursor = self.db.execute(
                        """
                        INSERT OR IGNORE INTO deliveries(subscription_id,event_key,card,created_at)
                        VALUES(?,?,?,?)
                    """,
                        (sub.id, key, card.to_json(), time.time()),
                    )
                    inserted += cursor.rowcount
                for key, value in (state or {}).items():
                    self.db.execute(
                        "INSERT INTO state(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
            return inserted

        return await self._run(operation)

    async def due(self, limit=20) -> list[dict]:
        def operation():
            rows = self.db.execute(
                """
                SELECT d.*, s.stream_id, s.options FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription_id
                WHERE d.status='pending' AND d.next_retry<=? ORDER BY d.id LIMIT ?
            """,
                (time.time(), limit),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(operation)

    async def current_subscription(self, subscription_id: int) -> Subscription | None:
        def operation():
            row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            return self._subscription(row) if row else None

        return await self._run(operation)

    async def delivered(self, delivery_id: int) -> None:
        def operation():
            with self.db:
                # Retain the deduplication key, release the potentially large card payload.
                self.db.execute("UPDATE deliveries SET status='sent',card='{}' WHERE id=?", (delivery_id,))

        await self._run(operation)

    async def failed(self, delivery_id: int, attempts: int) -> None:
        def operation():
            with self.db:
                self.db.execute(
                    "UPDATE deliveries SET attempts=attempts+1,next_retry=? WHERE id=?",
                    (time.time() + min(900, 5 * 2 ** min(attempts, 8)), delivery_id),
                )

        await self._run(operation)

    async def pending_count(self, stream_id: str = "") -> int:
        def operation():
            sql = (
                "SELECT COUNT(*) FROM deliveries d JOIN subscriptions s ON s.id=d.subscription_id "
                "WHERE d.status='pending'"
            )
            args = (stream_id,) if stream_id else ()
            return self.db.execute(sql + (" AND s.stream_id=?" if stream_id else ""), args).fetchone()[0]

        return await self._run(operation)

    async def prune(self) -> None:
        def operation():
            with self.db:
                # Keep dedup keys longer than the maximum configured catch-up horizon (7 days).
                self.db.execute(
                    "DELETE FROM deliveries WHERE created_at<? AND status='sent'", (time.time() - 30 * 86400,)
                )

        await self._run(operation)
