"""Durable alert intents and message IDs for the unattended runner."""

import asyncio
import json
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


# A summary job's states: being written by the model, written but not yet on the alert, on the
# alert, or failed (a later source's text may try again, up to the attempt limit)
RUNNING, WRITTEN, DELIVERED, FAILED = "running", "written", "delivered", "failed"


@dataclass(frozen=True)
class SummaryJob:
    """The summary of one release, kept across restarts so a delivered one isn't paid for and
    edited onto an old alert again (it was on Nov 12, twice after one restart)."""

    key: str
    document: str  # SHA-256 of the release text it was written from
    source: str  # where that text came from, such as "SEC 6-K"
    status: str
    attempts: int  # model calls made for this release
    text: str | None = None
    footer: str | None = None
    held_back: list[str] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    replaced: str | None = None  # source of the PDF-based summary this one replaced


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
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                channel_id INTEGER NOT NULL,
                release_key TEXT NOT NULL,
                document TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                text TEXT,
                footer TEXT,
                held_back TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                updated TEXT NOT NULL,
                PRIMARY KEY (channel_id, release_key)
            )
        """)
        # Model calls by day (US Eastern), for the daily cap on summary spend
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS summary_calls (
                channel_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                calls INTEGER NOT NULL,
                PRIMARY KEY (channel_id, day)
            )
        """)
        # The source of the PDF-based summary a page's summary replaced, if it replaced one
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(summaries)")}
        if "replaced" not in columns:
            self.db.execute("ALTER TABLE summaries ADD COLUMN replaced TEXT")
        # The bot's standing messages in this channel, such as the latest start notice
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS notices (
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (channel_id, name)
            )
        """)

    def notice(self, name: str) -> int | None:
        row = self.db.execute(
            "SELECT message_id FROM notices WHERE channel_id = ? AND name = ?",
            (self.channel_id, name),
        ).fetchone()
        return row["message_id"] if row else None

    def set_notice(self, name: str, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO notices VALUES (?, ?, ?)",
                (self.channel_id, name, message_id),
            )

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

    def summary(self, key: str) -> SummaryJob | None:
        row = self.db.execute(
            "SELECT * FROM summaries WHERE channel_id = ? AND release_key = ?",
            (self.channel_id, key),
        ).fetchone()
        if row is None:
            return None
        return SummaryJob(
            row["release_key"],
            row["document"],
            row["source"],
            row["status"],
            row["attempts"],
            row["text"],
            row["footer"],
            json.loads(row["held_back"]) if row["held_back"] else None,
            row["input_tokens"],
            row["output_tokens"],
            row["replaced"],
        )

    def summary_started(
        self, key: str, document: str, source: str, day: str, *, replaces: str | None = None
    ) -> None:
        """Records a model call starting for the release, from this document, and counts it
        toward the day's calls. `replaces` names the source of the summary it will replace."""
        now = datetime.now(UTC).isoformat()
        with self.db:
            self.db.execute(
                """
                INSERT INTO summaries (channel_id, release_key, document, source, status,
                                       attempts, updated, replaced)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (channel_id, release_key) DO UPDATE SET
                    document = excluded.document, source = excluded.source,
                    status = excluded.status, attempts = attempts + 1, text = NULL,
                    footer = NULL, held_back = NULL, input_tokens = NULL,
                    output_tokens = NULL, updated = excluded.updated,
                    replaced = excluded.replaced
                """,
                (self.channel_id, key, document, source, RUNNING, now, replaces),
            )
            self.db.execute(
                """
                INSERT INTO summary_calls VALUES (?, ?, 1)
                ON CONFLICT (channel_id, day) DO UPDATE SET calls = calls + 1
                """,
                (self.channel_id, day),
            )

    def summary_written(
        self,
        key: str,
        text: str,
        footer: str,
        held_back: list[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        with self.db:
            self.db.execute(
                """
                UPDATE summaries SET status = ?, text = ?, footer = ?, held_back = ?,
                    input_tokens = ?, output_tokens = ?, updated = ?
                WHERE channel_id = ? AND release_key = ?
                """,
                (
                    WRITTEN,
                    text,
                    footer,
                    json.dumps(held_back),
                    input_tokens,
                    output_tokens,
                    datetime.now(UTC).isoformat(),
                    self.channel_id,
                    key,
                ),
            )

    def summary_status(self, key: str, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE summaries SET status = ?, updated = ? WHERE channel_id = ? "
                "AND release_key = ?",
                (status, datetime.now(UTC).isoformat(), self.channel_id, key),
            )

    def summary_calls(self, day: str) -> int:
        row = self.db.execute(
            "SELECT calls FROM summary_calls WHERE channel_id = ? AND day = ?",
            (self.channel_id, day),
        ).fetchone()
        return row["calls"] if row else 0

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
