"""Durable alert intents and message IDs for the unattended runner."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import aiohttp
import discord


@dataclass(frozen=True)
class Delivery:
    key: str
    content: str
    prepared: datetime
    message_id: int | None


class DeliveryStore:
    def __init__(self, path: Path, channel_id: int) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # The store commits on the event loop, once right before each alert goes out and once
        # per schedule scan. The default journal syncs the disk at every commit, 2.4 ms here,
        # against 0.02 ms in WAL mode. WAL survives the bot crashing; NORMAL can only lose the
        # last commits if Windows itself goes down.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.channel_id = channel_id
        self._locks: dict[str, asyncio.Lock] = {}
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                channel_id INTEGER NOT NULL,
                release_key TEXT NOT NULL,
                content TEXT NOT NULL,
                prepared TEXT NOT NULL,
                message_id INTEGER,
                PRIMARY KEY (channel_id, release_key)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS windows (
                channel_id INTEGER NOT NULL,
                release_key TEXT NOT NULL,
                expected TEXT NOT NULL,
                PRIMARY KEY (channel_id, release_key, expected)
            )
        """)

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS watcher (
                channel_id INTEGER PRIMARY KEY,
                scanned TEXT NOT NULL
            )
        """)

    def last_scan(self) -> datetime | None:
        row = self.db.execute(
            "SELECT scanned FROM watcher WHERE channel_id = ?", (self.channel_id,)
        ).fetchone()
        return datetime.fromisoformat(row["scanned"]) if row else None

    def scanned(self, when: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO watcher VALUES (?, ?)",
                (self.channel_id, when.astimezone(UTC).isoformat()),
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.db.close()

    @staticmethod
    def _delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            row["release_key"],
            row["content"],
            datetime.fromisoformat(row["prepared"]),
            row["message_id"],
        )

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: str) -> Delivery | None:
        row = self.db.execute(
            "SELECT * FROM alerts WHERE channel_id = ? AND release_key = ?",
            (self.channel_id, key),
        ).fetchone()
        return self._delivery(row) if row else None

    def prepare(self, key: str, content: str) -> Delivery:
        prepared = datetime.now(UTC)
        with self.db:
            self.db.execute(
                "INSERT INTO alerts VALUES (?, ?, ?, ?, NULL)",
                (self.channel_id, key, content, prepared.isoformat()),
            )
        return Delivery(key, content, prepared, None)

    def forget(self, key: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM alerts WHERE channel_id = ? AND release_key = ?",
                (self.channel_id, key),
            )

    def delivered(self, key: str, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE alerts SET message_id = ? WHERE channel_id = ? AND release_key = ?",
                (message_id, self.channel_id, key),
            )

    def pending(self) -> list[Delivery]:
        rows = self.db.execute(
            "SELECT * FROM alerts WHERE channel_id = ? AND message_id IS NULL ORDER BY prepared",
            (self.channel_id,),
        )
        return [self._delivery(row) for row in rows]

    def window_closed(self, key: str, expected: datetime) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM windows WHERE channel_id = ? AND release_key = ? AND expected = ?",
                (self.channel_id, key, expected.astimezone(UTC).isoformat()),
            ).fetchone()
            is not None
        )

    def close_window(self, key: str, expected: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO windows VALUES (?, ?, ?)",
                (self.channel_id, key, expected.astimezone(UTC).isoformat()),
            )


async def retry_delivery[T](send: Callable[[], Awaitable[T]]) -> T:
    """Give a transient Discord error two more chances on the same delivery intent."""
    for delay in (1, 2):
        try:
            return await send()
        except discord.HTTPException as error:
            if error.status != 429 and error.status < 500:
                raise
        except (aiohttp.ClientError, TimeoutError):
            pass
        await asyncio.sleep(delay)
    return await send()
