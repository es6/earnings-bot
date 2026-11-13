"""Item 9: research continues during open release windows, bounded so monitoring keeps its
capacity. It stops in the minutes a release is most likely, never touches a company whose
own window is open, runs behind the polls, and leaves Chrome to live windows that use it."""

import asyncio
import io
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from earnings_bot import unattended
from earnings_bot.browser_sources import IRSource
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import HostBusy
from earnings_bot.pipeline import Releases
from earnings_bot.recorder import Journal, Target
from tests.fakes import RoutedFetcher
from tests.test_ir_release_coverage import company
from tests.test_unattended import FakePoster

HEADLINE = r"(?i).*third quarter 2026 results.*"


def target(ticker: str, cik: int, expected: datetime, *, chrome: bool = False) -> Target:
    ir = (
        IRSource(listing="https://investors.example.com/news", headline=HEADLINE)
        if chrome
        else None
    )
    return Target(
        ticker=ticker,
        cik=cik,
        expected=expected,
        before_minutes=60,
        after_minutes=90,
        period="FY2026Q3",
        ir=ir,
    )


async def test_research_pauses_only_in_the_minutes_around_a_release(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    now = journal.now()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path, fetcher, journal, Releases(FakePoster, fetcher, journal, None), store, print
        )
        # A window opened 30 minutes ago; its release is expected in 30 minutes
        runner.planned = {"a": target("AAA", 1, now + timedelta(minutes=30))}
        assert runner.busy() and not runner.research_paused()
        assert runner.in_window(1) and not runner.in_window(2)
        assert not runner.chrome_live()
        # Its release is expected in 5 minutes: research stops
        runner.planned = {"a": target("AAA", 1, now + timedelta(minutes=5))}
        assert runner.research_paused()
        # 40 minutes after the expected time, the rest of the window is research time again
        runner.planned = {"a": target("AAA", 1, now - timedelta(minutes=40))}
        assert runner.busy() and not runner.research_paused()
        # A window that reads an IR listing in Chrome keeps Chrome to itself
        runner.planned = {"a": target("AAA", 1, now + timedelta(minutes=30), chrome=True)}
        assert runner.chrome_live()
        with runner.research_mode():
            assert not fetcher.chrome_allowed()
            await fetcher.wait_turn("www.sec.gov")  # SEC still answers research, slowly
        runner.planned = {}
        assert not runner.busy()
        with runner.research_mode():
            assert fetcher.chrome_allowed()


async def test_research_permissions_follow_a_window_that_opens_after_the_task_started(
    tmp_path: Path,
) -> None:
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    now = journal.now()
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        runner = unattended.DirectoryWatcher(
            tmp_path, fetcher, journal, Releases(FakePoster, fetcher, journal, None), store, print
        )
        with runner.research_mode():
            assert fetcher.chrome_allowed()
            runner.planned = {"a": target("AAA", 1, now + timedelta(minutes=30), chrome=True)}
            assert not fetcher.chrome_allowed()
            runner.planned = {"a": target("AAA", 1, now + timedelta(minutes=5), chrome=True)}
            with pytest.raises(HostBusy):
                await fetcher.wait_turn("www.sec.gov")
            runner.planned = {}
            assert fetcher.research_deferred() and not fetcher.chrome_allowed()
        assert fetcher.chrome_allowed() and not fetcher.research_deferred()


async def test_date_discovery_researches_other_companies_behind_the_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ScanFinished(Exception):
        pass

    async def finished() -> None:
        raise ScanFinished

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        onon, _ = calendar.add("ONON", company("ONON"))
        sea, _ = calendar.add("SE", company("SE"))
        fetcher = RoutedFetcher({})
        await fetcher.wait_turn("investors.example.com")  # a live window's poll
        state = {"paused": False, "open": True}
        dates = DateDiscovery(
            calendar,
            fetcher,
            lambda: datetime.now(UTC),
            lambda: state["paused"],
            lambda _: None,
            windows_open=lambda: state["open"],
            chrome_live=lambda: True,
            in_window=lambda cik: cik == onon.cik,  # ONON's own window is open
        )
        checked: list[tuple[str, bool, bool]] = []

        async def run_once(row: object) -> None:
            ticker = row.ticker  # type: ignore[attr-defined]
            try:
                await fetcher.wait_turn("investors.example.com")
                live_host_answered = True
            except HostBusy:
                live_host_answered = False
            checked.append((ticker, fetcher.chrome_allowed(), live_host_answered))

        monkeypatch.setattr(dates, "run_once", run_once)
        monkeypatch.setattr(dates.changed, "wait", finished)
        with pytest.raises(ScanFinished):
            await dates.run()
        # Sea is researched during ONON's window, without Chrome and away from the live host
        assert checked == [("SE", False, False)]
        assert dates.phases.get(onon.cik) == "Waiting for its own release window to close"

        # In the minutes around a release nothing is researched at all
        state["paused"] = True
        dates.checked.clear()
        with pytest.raises(ScanFinished):
            await dates.run()
        assert checked == [("SE", False, False)]

        # With no window open, research runs normally
        state.update(paused=False, open=False)
        dates.checked.clear()
        dates.in_window = lambda cik: False
        with pytest.raises(ScanFinished):
            await dates.run()
        assert sorted(checked[1:]) == [("ONON", True, True), ("SE", True, True)]
        assert sea.cik in dates.checked


async def test_research_that_needs_chrome_waits_without_retrying_every_scan(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        # Last investigated months ago, so an investigation is due
        due = company("SE").model_copy(update={"investigated": date(2026, 6, 1)})
        row, _ = calendar.add("SE", due)
        fetcher = RoutedFetcher({})
        dates = DateDiscovery(
            calendar, fetcher, lambda: datetime.now(UTC), lambda: False, lambda _: None
        )
        dates.checked[row.cik] = time.monotonic() + 3600
        with fetcher.background(chrome=False):
            if not dates._chrome_waits():
                pytest.skip("Desktop Chrome research only runs on Windows")
            assert await dates._investigate(row) == row
        # Retried in 10 minutes, not on the next 60-second scan, and not in an hour either
        wait = dates.checked[row.cik] - time.monotonic()
        assert 500 < wait <= 600
        assert dates.phases[row.cik] == "Investigation waits for live Chrome windows to close"


@pytest.mark.parametrize("interruption", ["chrome", "http"])
async def test_an_interrupted_investigation_does_not_save_a_partial_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    from earnings_bot import date_discovery

    monkeypatch.setattr(date_discovery, "chrome_available", lambda: True)
    state = {"live": False}
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        previous = company("SE").model_copy(update={"investigated": date(2026, 6, 1)})
        row, _ = calendar.add("SE", previous)
        fetcher = RoutedFetcher({})
        dates = DateDiscovery(
            calendar,
            fetcher,
            lambda: datetime(2026, 11, 13, tzinfo=UTC),
            lambda: False,
            lambda _: None,
        )
        dates.checked[row.cik] = time.monotonic() + 3600

        async def interrupted(*args: object, **kwargs: object):
            if interruption == "chrome":
                state["live"] = True
            else:
                with pytest.raises(HostBusy):
                    await fetcher.wait_turn("investors.example.com")
            return previous.model_copy(update={"investigated": date(2026, 11, 13)})

        monkeypatch.setattr(date_discovery, "investigate", interrupted)
        if interruption == "http":
            await fetcher.wait_turn("investors.example.com")
        with fetcher.background(chrome=lambda: not state["live"]):
            assert await dates._investigate(row) == row
        saved = calendar.find("SE")
        assert saved is not None and saved.company.investigated == date(2026, 6, 1)
        assert 500 < dates.checked[row.cik] - time.monotonic() <= 600


def test_a_window_whose_routes_use_chrome_is_recognized() -> None:
    when = datetime(2026, 11, 17, 7, tzinfo=UTC)
    assert unattended.uses_chrome(target("AAA", 1, when, chrome=True))
    assert not unattended.uses_chrome(target("AAA", 1, when))
    assert asyncio  # keeps the import for the async tests above
