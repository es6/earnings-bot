import asyncio
import io
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest

from earnings_bot import unattended
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Fetcher
from earnings_bot.pipeline import DurableDiscordPoster, Release, Releases
from earnings_bot.recorder import ET, Journal, OnNew, Polls, Target, Watch
from earnings_bot.sources import EdgarFilings, EdgarLatest, RssFeed, Sighting
from tests.fakes import RoutedFetcher
from tests.test_pipeline import FakePoster, uber


def unknown_message() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(cast(Any, response), {"message": "Unknown Message", "code": 10008})


@dataclass
class FakeMessage:
    id: int
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    edited_at: datetime | None = None
    author: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=42))
    attachments: list[Any] = field(default_factory=list)
    embed: discord.Embed | None = None
    deleted: bool = False

    @property
    def embeds(self) -> list[discord.Embed]:
        return [self.embed] if self.embed else []

    async def edit(
        self,
        *,
        content: str | None = None,
        attachments: list[Any] | None = None,
        embed: discord.Embed | None = None,
    ) -> "FakeMessage":
        if self.deleted:
            raise unknown_message()
        if embed is not None:
            self.embed = embed
        if content is not None:
            self.content = content
        if attachments is not None:
            self.attachments = attachments
        self.edited_at = datetime.now(UTC)
        return self


class FakeDiscord:
    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.ids = itertools.count(1)  # never reused, like Discord's
        self.nonces: dict[str, FakeMessage] = {}
        self.attempts = 0
        self.fail_before_send = False
        self.lose_response = False

    async def send(
        self, content: str = "", *, nonce: str, embed: discord.Embed | None = None
    ) -> FakeMessage:
        self.attempts += 1
        if self.fail_before_send:
            self.fail_before_send = False
            raise TimeoutError("request timed out before delivery")
        if nonce in self.nonces:
            return self.nonces[nonce]
        message = FakeMessage(next(self.ids), content, embed=embed)
        self.messages[message.id] = self.nonces[nonce] = message
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("Discord accepted the message but the response was lost")
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if message_id not in self.messages:
            raise unknown_message()
        return self.messages[message_id]

    def delete(self, message_id: int) -> None:
        self.messages.pop(message_id).deleted = True

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


@pytest.mark.parametrize("reuse_poster", [False, True], ids=["fresh", "reused"])
async def test_an_alert_someone_deleted_is_posted_again(tmp_path: Path, reuse_poster: bool) -> None:
    target = uber()
    channel = FakeDiscord()
    feed = RssFeed("issuer", "https://issuer.example/rss")
    sighting = Sighting("one", "Quarterly results", "https://issuer.example/q3", earnings=True)
    journal = Journal(io.StringIO())
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        first = Release(
            target, channel.poster(store, target), RoutedFetcher({}), journal, None, None
        )
        first.add(feed, sighting)
        await first.finish()
        channel.delete(1)

        poster = first.poster if reuse_poster else channel.poster(store, target)
        again = Release(target, poster, RoutedFetcher({}), journal, None, None)
        again.add(feed, sighting)
        await again.finish()
        saved = store.get(target.release_key)

    assert again.posted
    assert list(channel.messages) == [2]
    assert saved is not None and saved.message_id == 2


async def test_a_resent_alert_keeps_links_edited_into_the_deleted_one(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    header = ["Earnings alert", "Timestamp"]
    first, edited, later = (
        f"{name}: <https://{name}.example/results>" for name in ("issuer", "wire", "sec")
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        poster = channel.poster(store, target)
        await poster.send("\n".join([*header, first]))
        await poster.edit("\n".join([*header, first, edited]))  # not in the saved first post
        channel.delete(1)

        await poster.send("\n".join([*header, later]))

    assert list(channel.messages) == [2]
    assert channel.messages[2].content.splitlines() == [*header, first, edited, later]


async def test_a_later_sighting_recreates_the_active_alert_and_keeps_links(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    feed = RssFeed("issuer", "https://issuer.example/rss")
    first = "https://issuer.example/results"
    second = "https://wire.example/results"
    journal = Journal(io.StringIO())
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        release = Release(
            target, channel.poster(store, target), RoutedFetcher({}), journal, None, None
        )
        release.add(feed, Sighting("one", "Results", first, earnings=True))
        await release.finish()
        channel.delete(1)
        release.add(feed, Sighting("two", "Results", second, earnings=True))
        await release.finish()
        saved = store.get(target.release_key)

    assert list(channel.messages) == [2]
    assert first in channel.messages[2].content and second in channel.messages[2].content
    assert saved is not None and saved.message_id == 2


@pytest.mark.parametrize("update", ["attachment", "summary"])
async def test_an_update_recreates_a_deleted_alert_and_is_applied(
    tmp_path: Path, update: str
) -> None:
    target = uber()
    channel = FakeDiscord()
    content = "Earnings alert\nTimestamp\nIssuer: <https://issuer.example/results>"
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        poster = channel.poster(store, target)
        await poster.send(content)
        channel.delete(1)

        if update == "attachment":
            await poster.attach("release.pdf", b"%PDF-1.7")
            assert channel.messages[2].attachments[0].filename == "release.pdf"
        else:
            await poster.set_summary("Results summary", "Source")
            embed = channel.messages[2].embed
            assert embed and embed.description == "Results summary"

        saved = store.get(target.release_key)

    assert list(channel.messages) == [2]
    assert channel.messages[2].content == content
    assert saved is not None and saved.message_id == 2


async def test_a_window_that_keeps_failing_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object) -> list[Watch]:
        raise RuntimeError("feed parser bug")

    monkeypatch.setattr(unattended, "record_target", broken)
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    status: list[str] = []
    (tmp_path / "a.toml").write_text(
        _plan("AAA", 1, datetime.now(UTC) + timedelta(minutes=5)), encoding="utf-8"
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None, None),
            store,
            status.append,
        )
        for _ in range(3):  # each scan retries the window that failed
            await runner.scan()
            await asyncio.gather(*(w.task for w in runner.windows.values()))

    assert status == [
        "⚠️ **AAA monitoring**: failed and will retry: RuntimeError('feed parser bug')"
    ]


async def test_a_resumed_alert_doesnt_attach_a_file_it_already_has(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    alert = "Earnings alert\nTimestamp\nIssuer: <https://issuer.example/earnings>"
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await channel.poster(store, target).send(alert)
        channel.messages[1].attachments = [SimpleNamespace(filename="release.pdf")]

        resumed = channel.poster(store, target)  # after a restart
        await resumed.send(alert)
        await resumed.attach("release.pdf", b"%PDF-1.7")

    assert resumed.resumed
    assert channel.messages[1].edited_at is None


async def test_a_resumed_alert_adds_only_the_links_it_lacks(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    folder = "https://www.sec.gov/Archives/edgar/data/1543151/000154315126000040"
    first = ["Alert", "Seen", "IR site: <https://ir.example/q3>", f"SEC 8-K: <{folder}/index.htm>"]
    wire = "Business Wire: <https://bw.example/q3>"
    # The other SEC source links the same filing's main document
    later = [*first[:3], f"SEC 8-K: <{folder}/uber-20261104.htm>", wire]
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await channel.poster(store, target).send("\n".join(first))
        resumed = channel.poster(store, target)  # after a restart
        await resumed.send("\n".join(first))
        await resumed.edit("\n".join(later))

    assert channel.messages[1].content.splitlines() == [*first, wire]


def test_the_store_commits_without_syncing_the_disk_each_time(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


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
            assert any(line.startswith("⚠️ **a.toml**:") for line in status)

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

    assert status == [
        "⌛ **GAP**: window ended while the bot was off, and no completed watch was recorded"
    ]


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


async def test_two_schedules_defining_one_release_use_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []

    async def watch(target: Target, *args: object) -> list[Watch]:
        started.append(target.ticker)
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(unattended, "record_target", watch)
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    when = datetime.now(UTC) + timedelta(hours=1)
    # Same CIK and day, so the same release, under two tickers
    (tmp_path / "a.toml").write_text(_plan("GOOGL", 1652044, when), encoding="utf-8")
    (tmp_path / "b.toml").write_text(_plan("GOOG", 1652044, when), encoding="utf-8")
    status: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None, None),
            store,
            status.append,
        )
        try:
            await runner.scan()
            await asyncio.sleep(0)
        finally:
            for window in runner.windows.values():
                window.task.cancel()
            await asyncio.gather(
                *(window.task for window in runner.windows.values()), return_exceptions=True
            )

    assert started == ["GOOGL"]
    assert status[-1].endswith("b.toml also defines GOOG. Using the first schedule.")


async def test_a_finished_window_isnt_watched_again_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []

    async def watch(target: Target, *args: object) -> list[Watch]:
        started.append(target.ticker)
        return []

    monkeypatch.setattr(unattended, "record_target", watch)
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    when = datetime.now(UTC) + timedelta(minutes=5)
    (tmp_path / "a.toml").write_text(_plan("AAA", 1, when), encoding="utf-8")
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        for _ in range(2):  # the second watcher is the bot after a restart
            runner = unattended.DirectoryWatcher(
                tmp_path,
                fetcher,
                journal,
                Releases(FakePoster, fetcher, journal, None, None),
                store,
                lambda message: None,
            )
            await runner.scan()
            await asyncio.gather(*(w.task for w in runner.windows.values()))

    assert started == ["AAA"]


async def test_an_alert_from_before_the_new_layout_still_takes_only_new_links(
    tmp_path: Path,
) -> None:
    target = uber()
    channel = FakeDiscord()
    folder = "https://www.sec.gov/Archives/edgar/data/1543151/000154315126000040"
    # Posted by the bot before links were masked
    old = ["Alert", "Seen", "IR site: <https://ir.example/q3>", f"SEC 8-K: <{folder}/index.htm>"]
    wire = "📰 [Business Wire](<https://bw.example/q3>)"
    new = [
        "Alert",
        "Seen",
        "🌐 [IR site](<https://ir.example/q3>)",
        f"🏛️ [SEC 8-K](<{folder}/uber-20261104.htm>)",
        wire,
    ]
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await channel.poster(store, target).send("\n".join(old))
        resumed = channel.poster(store, target)  # after a restart into the new layout
        await resumed.send("\n".join(old))
        await resumed.edit("\n".join(new))

    assert channel.messages[1].content.splitlines() == [*old, wire]


def test_a_window_report_names_the_winner_then_how_far_behind_the_rest_were() -> None:
    target = Target(
        ticker="LCID",
        expected=datetime(2026, 11, 9, 16, 5, tzinfo=ET),
        cik=1811210,
        period="FY2026Q3",
    )
    first = datetime(2026, 11, 9, 16, 6, 2, 708_000, tzinfo=ET)
    fresh = RssFeed("prnewswire_busted", "https://www.prnewswire.com/rss/x.rss")
    cached = RssFeed("prnewswire", "https://www.prnewswire.com/rss/x.rss")
    window = target.window
    watches = [
        Watch("LCID", fresh, window, first=first),
        Watch("LCID", cached, window, first=first + timedelta(seconds=0.4)),
        Watch("LCID", EdgarLatest(1811210), window, first=first + timedelta(seconds=171.31)),
        Watch("LCID", EdgarFilings(1811210), window, stopped="HTTP 403"),
    ]

    report = unattended._window_report(target, watches, "@earnings_guy: no post found")

    assert report.splitlines() == [
        "🏁 **LCID Q3 FY26 window closed**",
        "🥇 PR Newswire earnings feed at 4:06:02.708 PM ET",
        "Then SEC 8-K filings +2m 51s",
        "⚠️ Stopped early: SEC 8-K filings",
        "-# @earnings_guy: no post found",
    ]
    caught = [Watch("LCID", fresh, window, caught_up="lucid-q3")]
    assert unattended._window_report(target, caught).splitlines()[1] == (
        "Already out when the window opened: PR Newswire earnings feed"
    )
    assert unattended._window_report(target, []).splitlines()[1] == "No release seen"
