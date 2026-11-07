import asyncio
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import discord
import pytest

from earnings_bot import unattended
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Fetcher
from earnings_bot.pipeline import DurableDiscordPoster, Release, Releases
from earnings_bot.recorder import Journal, OnNew, Polls, Target, Watch
from earnings_bot.sources import RssFeed, Sighting
from tests.fakes import RoutedFetcher
from tests.test_pipeline import FakePoster, uber


@dataclass
class FakeMessage:
    id: int
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    edited_at: datetime | None = None
    author: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=42))

    async def edit(self, *, content: str) -> "FakeMessage":
        self.content = content
        self.edited_at = datetime.now(UTC)
        return self


class FakeDiscord:
    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.nonces: dict[str, FakeMessage] = {}
        self.attempts = 0
        self.fail_before_send = False
        self.lose_response = False

    async def send(self, content: str, *, nonce: str) -> FakeMessage:
        self.attempts += 1
        if self.fail_before_send:
            self.fail_before_send = False
            raise TimeoutError("request timed out before delivery")
        if nonce in self.nonces:
            return self.nonces[nonce]
        message = FakeMessage(len(self.messages) + 1, content)
        self.messages[message.id] = self.nonces[nonce] = message
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("Discord accepted the message but the response was lost")
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        return self.messages[message_id]

    async def history(self, *, limit: int | None, after: datetime) -> AsyncIterator[FakeMessage]:
        for message in self.messages.values():
            if message.created_at > after:
                yield message

    def poster(self, store: DeliveryStore, target: Target) -> DurableDiscordPoster:
        return DurableDiscordPoster(
            cast(discord.abc.Messageable, self), store, target.release_key, 42
        )


async def test_an_alert_retries_then_resumes_after_restart(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    channel.fail_before_send = True
    db = tmp_path / "state.db"
    ir = RssFeed("issuer", "https://issuer.example/rss")
    first = Sighting("one", "Quarterly results", "https://issuer.example/earnings", earnings=True)
    second = Sighting("two", "Quarterly results", "https://wire.example/earnings", earnings=True)
    journal = Journal(io.StringIO())
    with DeliveryStore(db, 7) as store:
        release = Release(
            target, channel.poster(store, target), RoutedFetcher({}), journal, None, None
        )
        release.add(ir, first)
        await release.finish()
        saved = store.get(target.release_key)
        assert release.posted and saved is not None and saved.message_id == 1

    assert channel.attempts == 2
    first_alert_header = channel.messages[1].content.splitlines()[:2]
    with DeliveryStore(db, 7) as store:
        resumed = Release(
            target, channel.poster(store, target), RoutedFetcher({}), journal, None, None
        )
        resumed.add(ir, second)
        await resumed.finish()

    assert channel.attempts == 2
    assert list(channel.messages) == [1]
    content = channel.messages[1].content
    assert content.splitlines()[:2] == first_alert_header
    assert first.url in content and second.url in content


async def test_restart_recovers_message_accepted_before_the_id_was_saved(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    channel.lose_response = True
    db = tmp_path / "state.db"
    content = "Earnings alert\nTimestamp\nIssuer: <https://issuer.example/earnings>"
    with DeliveryStore(db, 7) as store:
        with pytest.raises(TimeoutError):
            await channel.poster(store, target).send(content)
        assert len(store.pending()) == 1

    with DeliveryStore(db, 7) as store:
        poster = channel.poster(store, target)
        assert await poster.send(content) is None
        assert poster.resumed
        assert not store.pending()
    assert channel.attempts == 1


def _plan(ticker: str, cik: int, when: datetime) -> str:
    return f'[[target]]\nticker = "{ticker}"\ncik = {cik}\nexpected = {when.isoformat()}\n'


async def test_schedule_reload_preserves_running_windows_and_shares_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Target, Polls | None]] = []

    async def watch(
        target: Target,
        fetcher: Fetcher,
        journal: Journal,
        on_new: OnNew | None = None,
        polls: Polls | None = None,
    ) -> list[Watch]:
        started.append((target, polls))
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(unattended, "record_target", watch)
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    releases = Releases(FakePoster, fetcher, journal, None, None)
    status: list[str] = []
    when = datetime.now(UTC) + timedelta(hours=1)
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text(_plan("AAA", 1, when), encoding="utf-8")

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path, fetcher, journal, releases, store, status.append
        )
        try:
            await runner.scan()
            await asyncio.sleep(0)
            original = runner.windows[started[0][0].release_key].task
            a.write_text("invalid schedule", encoding="utf-8")
            b.write_text(_plan("BBB", 2, when), encoding="utf-8")
            await runner.scan()
            await asyncio.sleep(0)
            assert [t.ticker for t, _ in started] == ["AAA", "BBB"]
            assert all(polls is runner.polls for _, polls in started)
            assert not original.done()
            assert any(line.startswith("a.toml:") for line in status)

            a.unlink()
            await runner.scan()
            assert original.cancelled()
            assert [window.target.ticker for window in runner.windows.values()] == ["BBB"]
        finally:
            for window in runner.windows.values():
                window.task.cancel()
            await asyncio.gather(
                *(window.task for window in runner.windows.values()), return_exceptions=True
            )


async def test_restart_reports_windows_that_closed_while_offline(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    (tmp_path / "old.toml").write_text(_plan("OLD", 1, now - timedelta(days=2)), encoding="utf-8")
    (tmp_path / "missed.toml").write_text(
        _plan("GAP", 2, now - timedelta(hours=2)), encoding="utf-8"
    )
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    releases = Releases(FakePoster, fetcher, journal, None, None)
    status: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.scanned(now - timedelta(hours=3))
        for _ in range(2):
            watcher = unattended.DirectoryWatcher(
                tmp_path, fetcher, journal, releases, store, status.append
            )
            await watcher.scan()

    assert status == ["GAP: window expired and no completed watch was recorded"]


async def test_a_twenty_release_burst_keeps_one_message_per_release_after_restart(
    tmp_path: Path,
) -> None:
    channel = FakeDiscord()
    channel.lose_response = True
    db = tmp_path / "state.db"
    feed = RssFeed("issuer", "https://issuer.example/rss")
    targets = [
        uber().model_copy(update={"ticker": f"CO{i}", "cik": i, "period": "FY2026Q3"})
        for i in range(1, 21)
    ]
    for source_name in ("issuer", "wire"):
        with DeliveryStore(db, 7) as store:
            journal = Journal(io.StringIO())
            releases = Releases(
                FakePoster,
                RoutedFetcher({}),
                journal,
                None,
                None,
                lambda target: channel.poster(store, target),
            )
            for target in targets:
                url = f"https://{source_name}.example/earnings/{target.ticker}"
                releases.on_new(target, feed, Sighting(url, "Results", url, earnings=True))
            await releases.finish()

            assert not store.pending()
            assert all((row := store.get(t.release_key)) and row.message_id for t in targets)
    assert len(channel.messages) == 20
    assert channel.attempts == 20
    assert all("https://wire.example" in message.content for message in channel.messages.values())
