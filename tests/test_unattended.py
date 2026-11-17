import asyncio
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import discord
import pytest

from first_print import delivery, pipeline, recorder, unattended
from first_print.browser_sources import BrowserRelease
from first_print.delivery import DeliveryStore
from first_print.fetch import Fetcher, Response
from first_print.formatting import AMBER, clock, day
from first_print.known import to_target
from first_print.pipeline import Release, Releases
from first_print.recorder import ET, Journal, OnNew, Polls, Target, Watch
from first_print.release_cards import release_content
from first_print.sources import EdgarFilings, EdgarLatest, GuessedFile, RssFeed, Sighting
from first_print.track_progress import TrackProgress
from first_print.unattended import DirectoryWatcher, _Window
from tests.builders import TRACKING_NOW as NOW
from tests.builders import uber
from tests.fakes import (
    FakeDiscord,
    FakePoster,
    FixedClock,
    PinnedClock,
    RoutedFetcher,
    embed_text,
)


async def test_an_alert_retries_then_resumes_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "RETRY_DELAYS", (0.0, 0.0))
    target = uber()
    channel = FakeDiscord()
    channel.fail_before_send = True
    db = tmp_path / "state.db"
    ir = RssFeed("issuer", "https://issuer.example/rss")
    first = Sighting("one", "Quarterly results", "https://issuer.example/earnings", earnings=True)
    second = Sighting("two", "Quarterly results", "https://wire.example/earnings", earnings=True)
    journal = Journal(io.StringIO())
    with DeliveryStore(db, 7) as store:
        release = Release(target, channel.poster(store, target), RoutedFetcher({}), journal, None)
        release.add(ir, first)
        await release.finish()
        saved = store.get(target.release_key)
        assert release.posted and saved is not None and saved.message_id == 1

    assert channel.attempts == 2
    first_alert_header = release_content(channel.messages[1]).splitlines()[:2]
    with DeliveryStore(db, 7) as store:
        resumed = Release(target, channel.poster(store, target), RoutedFetcher({}), journal, None)
        resumed.add(ir, second)
        await resumed.finish()

    assert channel.attempts == 2
    assert list(channel.messages) == [1]
    content = release_content(channel.messages[1])
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
        first = Release(target, channel.poster(store, target), RoutedFetcher({}), journal, None)
        first.add(feed, sighting)
        await first.finish()
        channel.delete(1)

        poster = first.poster if reuse_poster else channel.poster(store, target)
        again = Release(target, poster, RoutedFetcher({}), journal, None)
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
        release = Release(target, channel.poster(store, target), RoutedFetcher({}), journal, None)
        release.add(feed, Sighting("one", "Results", first, earnings=True))
        await release.finish()
        channel.delete(1)
        release.add(feed, Sighting("two", "Results", second, earnings=True))
        await release.finish()
        saved = store.get(target.release_key)

    assert list(channel.messages) == [2]
    assert first in release_content(channel.messages[2])
    assert second in release_content(channel.messages[2])
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
    async def broken(*args: object, **kwargs: object) -> list[Watch]:
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
            Releases(FakePoster, fetcher, journal, None),
            store,
            status.append,
        )
        for _ in range(3):  # each scan retries the window that failed
            await runner.scan()
            await asyncio.gather(*(w.task for w in runner.windows.values()))

    assert status == [
        "⚠️ **AAA monitoring**: failed and will retry: RuntimeError('feed parser bug')"
    ]


FOLDER = "https://www.sec.gov/Archives/edgar/data/1543151/000154315126000040"
PRN = "https://www.prnewswire.com/news-releases/example-earnings-"


@pytest.mark.parametrize(
    ("first", "later", "added"),
    [
        pytest.param(
            [
                "Alert",
                "Seen",
                "IR site: <https://ir.example/q3>",
                f"SEC 8-K: <{FOLDER}/index.htm>",
                f"PR Newswire: <{PRN}302934228.html>",
            ],
            [
                "Alert",
                "Seen",
                "IR site: <https://ir.example/q3>",
                # The other SEC source links the same filing's main document
                f"SEC 8-K: <{FOLDER}/uber-20261104.htm>",
                f"PR Newswire: <{PRN}302934067.html>",
                "Business Wire: <https://bw.example/q3>",
            ],
            ["Business Wire: <https://bw.example/q3>"],
            id="same-layout",
        ),
        pytest.param(
            # Posted by the bot before links were masked
            ["Alert", "Seen", "IR site: <https://ir.example/q3>", f"SEC 8-K: <{FOLDER}/index.htm>"],
            [
                "Alert",
                "Seen",
                "🌐 [IR site](<https://ir.example/q3>)",
                f"🏛️ [SEC 8-K](<{FOLDER}/uber-20261104.htm>)",
                "📰 [Business Wire](<https://bw.example/q3>)",
            ],
            ["📰 [Business Wire](<https://bw.example/q3>)"],
            id="before-the-masked-layout",
        ),
    ],
)
async def test_a_resumed_alert_adds_only_the_links_it_lacks(
    tmp_path: Path, first: list[str], later: list[str], added: list[str]
) -> None:
    target = uber()
    channel = FakeDiscord()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await channel.poster(store, target).send("\n".join(first))
        resumed = channel.poster(store, target)  # after a restart
        await resumed.send("\n".join(first))
        await resumed.edit("\n".join(later))

    assert channel.messages[1].content.splitlines() == [*first, *added]


async def test_links_merged_into_a_resumed_alert_are_cut_to_discords_limit(tmp_path: Path) -> None:
    target = uber()
    channel = FakeDiscord()
    first = ["Alert", "Seen", "IR site: <https://ir.example/q3>"]
    many = [*first, *(f"Wire {n}: <https://wire.example/{n}/{'x' * 60}>" for n in range(40))]
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await channel.poster(store, target).send("\n".join(first))
        resumed = channel.poster(store, target)  # after a restart
        await resumed.send("\n".join(first))
        await resumed.edit("\n".join(many))

    merged = channel.messages[1].content
    assert len(merged) == pipeline.MAX_CONTENT
    assert merged.endswith("…")


def test_the_store_commits_without_syncing_the_disk_each_time(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


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
        **kwargs: object,
    ) -> list[Watch]:
        started.append((target, polls))
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(unattended, "record_target", watch)
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    releases = Releases(FakePoster, fetcher, journal, None)
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
    releases = Releases(FakePoster, fetcher, journal, None)
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


async def test_calendar_updates_edit_one_card_and_keep_their_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def watch(*args: object, **kwargs: object) -> list[Watch]:
        await asyncio.Event().wait()
        return []

    async def audit(*args: object) -> Any:
        raise AssertionError("This test only publishes cards")

    monkeypatch.setattr(unattended, "record_target", watch)
    fetcher = RoutedFetcher({})
    file = io.StringIO()
    journal = Journal(file)
    standalone: list[str] = []
    channel = FakeDiscord()
    when = datetime.now(UTC) + timedelta(days=1)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None),
            store,
            standalone.append,
        )
        row, _ = runner.calendar.add("NVDA")
        progress = TrackProgress(
            runner.calendar,
            store,
            cast(discord.abc.Messageable, channel),
            42,
            lambda: runner.planned,
            journal.now,
            runner.busy,
            audit,
            card_spacing=0.0,
        )
        runner.progress = progress
        try:
            for hour, evidence in [
                (0, "First notice"),
                (0, "Updated source link"),
                (1, "New time"),
            ]:
                target = Target(ticker="NVDA", cik=row.cik, expected=when + timedelta(hours=hour))
                runner.calendar.set_target(target, evidence)
                runner.dates.status(f"📅 NVDA · {evidence}")
                assert progress.changed.is_set()
                progress.changed.clear()
                await runner.scan()
                [current] = runner.calendar.entries()
                await progress.publish(row.cik, progress.render(current, None, False))
                await asyncio.sleep(0)
            assert standalone == []
            [message] = channel.messages.values()
            assert channel.attempts == 1 and message.edited_at is not None
            assert message.embed and "New time" in (message.embed.description or "")
            # Errors still get their own notification.
            runner.directory = tmp_path / "gone"
            await runner.scan()
            [problem] = standalone
            assert problem.startswith("⚠️ **Schedule directory**: ")
        finally:
            for window in runner.windows.values():
                window.task.cancel()
            await asyncio.gather(*(w.task for w in runner.windows.values()), return_exceptions=True)

    records = [json.loads(line) for line in file.getvalue().splitlines()]
    inline = [r for r in records if r.get("inline")]
    assert [r["message"] for r in inline if r["message"].startswith("📅")] == [
        "📅 NVDA · First notice",
        "📅 NVDA · Updated source link",
        "📅 NVDA · New time",
    ]
    assert any("schedule changed" in r["message"] for r in inline)


async def test_a_twenty_release_burst_keeps_one_message_per_release_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "RETRY_DELAYS", (0.0, 0.0))
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
    assert all("https://wire.example" in release_content(m) for m in channel.messages.values())


async def test_two_schedules_defining_one_release_use_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []

    async def watch(target: Target, *args: object, **kwargs: object) -> list[Watch]:
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
            Releases(FakePoster, fetcher, journal, None),
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

    async def watch(target: Target, *args: object, **kwargs: object) -> list[Watch]:
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
                Releases(FakePoster, fetcher, journal, None),
                store,
                lambda message: None,
            )
            await runner.scan()
            await asyncio.gather(*(w.task for w in runner.windows.values()))

    assert started == ["AAA"]


LCID = Target(
    ticker="LCID",
    expected=datetime(2026, 11, 9, 16, 5, tzinfo=ET),
    cik=1811210,
    period="FY2026Q3",
)
FIRST_LCID = datetime(2026, 11, 9, 16, 6, 2, 708_000, tzinfo=ET)
FRESH_FEED = RssFeed("prnewswire_busted", "https://www.prnewswire.com/rss/x.rss")


def test_a_window_report_names_the_winner_then_how_far_behind_the_rest_were() -> None:
    cached = RssFeed("prnewswire", "https://www.prnewswire.com/rss/x.rss")
    window = LCID.window
    watches = [
        Watch("LCID", FRESH_FEED, window, first=FIRST_LCID),
        Watch("LCID", cached, window, first=FIRST_LCID + timedelta(seconds=0.4)),
        Watch("LCID", EdgarLatest(1811210), window, first=FIRST_LCID + timedelta(seconds=171.31)),
        Watch("LCID", EdgarFilings(1811210), window, stopped="HTTP 403"),
    ]

    report = unattended._window_report(LCID, watches, "@earnings_guy: no post found")

    assert report.splitlines() == [
        "🏁 **LCID Q3 FY26 window closed** · ⚠️ degraded",
        "🥇 PR Newswire earnings feed at 4:06:02.708 PM ET",
        "Then SEC 8-K filings +2m 51s",
        "⚠️ Stopped early: SEC 8-K filings",
        "-# @earnings_guy: no post found",
    ]


def test_a_route_the_site_refused_at_times_is_named_unless_it_found_the_release() -> None:
    window = LCID.window
    refused_sec = Watch("LCID", EdgarFilings(1811210), window, blocked=3)
    refused_wire = Watch("LCID", FRESH_FEED, window, blocked=1, first=FIRST_LCID)

    report = unattended._window_report(LCID, [refused_wire, refused_sec])

    assert report.splitlines()[-1] == "⚠️ Refused at times (HTTP 403), retried: SEC 8-K filings"


def test_a_window_report_says_which_sources_had_the_release_already_out() -> None:
    caught = [Watch("LCID", FRESH_FEED, LCID.window, caught_up="lucid-q3")]

    report = unattended._window_report(LCID, caught)

    assert report.splitlines()[1] == "Already out when the window opened: PR Newswire earnings feed"


def test_a_window_report_with_no_release_says_so() -> None:
    assert unattended._window_report(LCID, []).splitlines()[1] == "No release seen"


async def test_a_restart_resumes_later_links_without_a_second_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = FakeDiscord()
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO(), PinnedClock())
    when = journal.now() - timedelta(minutes=31)
    target = Target(ticker="SE", cik=1703399, expected=when)
    (tmp_path / "a.toml").write_text(_plan("SE", target.cik, when), encoding="utf-8")
    first = "Alert\nSeen\nIR site: <https://example.com/results>"
    late = "https://example.com/late-6k"

    async def follow(
        planned: Target,
        watches: list[Watch],
        fetch: Fetcher,
        journal: Journal,
        on_new: OnNew,
        *args: object,
    ) -> None:
        on_new(
            planned,
            EdgarFilings(planned.cik, form="6-K"),
            Sighting("6-K", "Results", late, earnings=True),
        )

    monkeypatch.setattr(unattended, "follow_up", follow)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        await channel.poster(store, target).send(first)
        store.close_window(target.release_key, target.expected)
        runner = unattended.DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(
                FakePoster,
                fetcher,
                journal,
                None,
                lambda current: channel.poster(store, current),
            ),
            store,
            lambda _: None,
        )
        runner.follow_up = timedelta(hours=1)
        await runner.scan()
        await asyncio.gather(*(w.task for w in runner.windows.values()))
        await runner.scan()
        assert not runner.windows

    assert len(channel.messages) == 1
    assert "https://example.com/results" in release_content(channel.messages[1])
    assert late in release_content(channel.messages[1])


async def test_changed_sources_rearm_an_active_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Journal(io.StringIO(), PinnedClock())
    when = journal.now() - timedelta(minutes=31)
    target = Target(ticker="SE", cik=1703399, expected=when)
    plan = tmp_path / "a.toml"
    plan.write_text(_plan("SE", target.cik, when), encoding="utf-8")
    fetcher = RoutedFetcher({})
    started: list[Target] = []

    async def resumed(planned: Target) -> None:
        started.append(planned)
        await asyncio.Event().wait()

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        store.prepare(target.release_key, "Already delivered")
        store.delivered(target.release_key, 1)
        store.close_window(target.release_key, target.expected)
        runner = unattended.DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None),
            store,
            lambda _: None,
        )
        runner.follow_up = timedelta(hours=1)
        monkeypatch.setattr(runner, "_resume_links", resumed)
        try:
            await runner.scan()
            await asyncio.sleep(0)
            plan.write_text(
                _plan("SE", target.cik, when) + 'prnewswire = ["Sea Limited"]\n', encoding="utf-8"
            )
            await runner.scan()
            await asyncio.sleep(0)
            assert [watch.prnewswire for watch in started] == [[], ["Sea Limited"]]
        finally:
            for window in runner.windows.values():
                window.task.cancel()
            await asyncio.gather(
                *(window.task for window in runner.windows.values()), return_exceptions=True
            )


async def test_a_resumed_alert_gives_no_time_for_a_link_it_already_had(tmp_path: Path) -> None:
    """After the Nov 12 restart: "NBIS: added the IR site link, live after -15761.29s". The
    link was already on the hours-old alert, so nothing went live then."""
    target, channel, log = uber(), FakeDiscord(), io.StringIO()
    feed = RssFeed("issuer", "https://issuer.example/rss")
    sightings = [
        Sighting("1", "Quarterly results", "https://issuer.example/q3", earnings=True),
        Sighting("2", "Quarterly results", "https://wire.example/q3", earnings=True),
    ]
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        release = Release(
            target, channel.poster(store, target), RoutedFetcher({}), Journal(io.StringIO()), None
        )
        for sighting in sightings:
            release.add(feed, sighting)
        await release.finish()
        alert = channel.messages[1]
        assert alert.edited_at is not None
        alert.edited_at -= timedelta(hours=4)

        resumed = Release(
            target, channel.poster(store, target), RoutedFetcher({}), Journal(log), None
        )
        for sighting in sightings:
            resumed.add(feed, sighting)
        await resumed.finish()

    linked = [r for r in map(json.loads, log.getvalue().splitlines()) if r["event"] == "linked"]
    assert linked
    assert [r["live_seconds"] for r in linked] == [None] * len(linked)


async def test_a_start_notice_replaces_the_ones_before_it(tmp_path: Path) -> None:
    """Each deploy left another "Earnings monitoring started" message in the channel."""
    channel = FakeDiscord()
    old = await channel.send("🟢 **Earnings monitoring started** · v0.15.3 · `schedules/`")
    alert = await channel.send("🚨 **$AMAT Q4 FY26 earnings are out**", nonce="alert")
    with DeliveryStore(tmp_path / "progress.db", 7) as store:
        messages = cast(discord.abc.Messageable, channel)
        await unattended.replace_start_notice(
            messages, store, 42, "🟢 **Earnings monitoring started** · v0.19.3"
        )
        [first] = [m for m in channel.messages.values() if m.content.endswith("v0.19.3")]
        assert old.deleted and not first.deleted
        await unattended.replace_start_notice(
            messages, store, 42, "🟢 **Earnings monitoring started** · v0.19.4"
        )

    shown = [m.content for m in channel.messages.values() if not m.deleted]
    assert shown == [alert.content, "🟢 **Earnings monitoring started** · v0.19.4"]
    assert first.deleted


async def test_a_resumed_alert_doesnt_attach_its_file_again(tmp_path: Path) -> None:
    """Nebius's alert had its PDF three times by the evening of Nov 12, one more per restart:
    Discord stored "Nebius%20Q3...pdf" as "Nebius20Q3...pdf", so the check for a file
    already on the alert never matched."""
    target, channel = uber(), FakeDiscord()
    url = "https://nebius.com/files/Nebius%20Q3%202026%20Results.pdf"
    sighting = Sighting(url, "Nebius Q3 2026 Results", url, earnings=True)
    fetcher = RoutedFetcher({".pdf": (200, b"%PDF-1.7 results")})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        for _ in range(3):  # the first start, then two restarts
            release = Release(
                target, channel.poster(store, target), fetcher, Journal(io.StringIO()), None
            )
            release.add(GuessedFile(url), sighting)
            await release.finish()

    [alert] = channel.messages.values()
    assert [a.filename for a in alert.attachments] == ["Nebius_Q3_2026_Results.pdf"]


@pytest.fixture
def watcher(tmp_path: Path) -> Iterator[DirectoryWatcher]:
    """A directory watcher with no schedule files, as the bot builds it, at a fixed time."""
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO(), FixedClock(NOW))
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        yield DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None),
            store,
            lambda message: None,
        )


async def test_watch_status_shows_each_releases_time_beside_when_its_watch_opens(
    watcher: DirectoryWatcher,
) -> None:
    # ONON, Nov 10: /watch_status said "from 5:10 AM" and /upcoming "6:10 AM", which read as a
    # disagreement. One was the watch opening, the other the release.
    onon = to_target("HD", NOW + timedelta(hours=2), 2026, 3)
    onon.before_minutes, onon.after_minutes = 60, 90
    task = asyncio.create_task(asyncio.sleep(3600))
    watcher.windows[onon.release_key] = _Window(onon, task)
    try:
        embed = watcher.health_embed("v0.8.0", "abc1234")
    finally:
        task.cancel()

    # /status is health: the next watch's start, not a second calendar (that's /upcoming)
    text = embed_text(embed)
    assert embed.title == "First Print v0.8.0 · status"
    assert f"⏭️ Next watch: **HD** from {day(onon.window[0])}, {clock(onon.window[0])}" in text
    assert "results about" not in text
    assert "📝 Live summaries off" in text
    assert embed.footer.text == "Build abc1234"


def test_a_route_that_went_quiet_marks_the_window_report_degraded() -> None:
    """Nebius's newsroom was refused at 6:59 AM on Nov 12 and went unanswered for the rest of
    the window. A route dying mid-window must be loud (the 1.0 gate)."""
    expected = datetime(2026, 11, 12, 8, 0, tzinfo=ET)
    target = to_target("HD", expected, 2026, 3)
    start = target.window[0]
    listing = BrowserRelease("https://ir.example.com/news", "third quarter")
    quiet = Watch("HD", listing, target.window, previous=start)
    edgar = Watch("HD", EdgarFilings(354950), target.window, previous=start)
    answers = (
        (quiet, [start + timedelta(seconds=s) for s in (1, 2, 200)]),
        (edgar, [start + timedelta(seconds=s) for s in range(1, 200, 3)]),
    )
    for w, times in answers:
        for sent in times:
            recorder.take(
                w, sent, Response("", 200, {}, b"", 0.01), Journal(io.StringIO()), items=[]
            )

    report = unattended._window_report(target, [quiet, edgar])

    assert report.splitlines()[0] == "🏁 **HD Q3 FY26 window closed** · ⚠️ degraded"
    assert (
        "⚠️ Went without an answer for up to: IR press release page, read in Chrome 3m 18s" in report
    )
    assert "SEC 8-K" not in report.splitlines()[-1]


async def test_status_names_a_degraded_route_while_its_window_is_open(
    watcher: DirectoryWatcher,
) -> None:
    target = to_target("HD", NOW + timedelta(minutes=5), 2026, 3)
    start = target.window[0]
    refused = "HTTP 403: the site refused us. Trying again after a pause"
    listing = Watch("HD", BrowserRelease("https://ir.example.com/news", "q3"), target.window)
    listing.previous, listing.problem = start, refused
    edgar = Watch("HD", EdgarFilings(354950), target.window, previous=NOW)
    task = asyncio.create_task(asyncio.sleep(3600))
    watcher.windows[target.release_key] = _Window(target, task)
    watcher.live_watches[target.release_key] = [listing, edgar]
    try:
        embed = watcher.health_embed("v0.19.5", None)
    finally:
        task.cancel()

    text = embed_text(embed)
    assert "⚠️ Degraded: IR press release page, read in Chrome, no answer for" in text
    assert refused in text
    assert "Degraded: SEC" not in text
    assert embed.color is not None and embed.color.value == AMBER
