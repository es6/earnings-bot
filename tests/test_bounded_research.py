"""Item 9: research continues during open release windows, bounded so monitoring keeps its
capacity. It stops in the minutes a release is most likely, never touches a company whose
own window is open, runs behind the polls, and leaves Chrome to live windows that use it."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from first_print import date_discovery, fetch, unattended
from first_print.browser_sources import IRSource
from first_print.calendar import Calendar
from first_print.date_discovery import DateDiscovery
from first_print.delivery import DeliveryStore
from first_print.fetch import HostBusy
from first_print.pipeline import Releases
from first_print.recorder import Journal, Target
from tests.builders import company
from tests.fakes import FakePoster, RoutedFetcher, ScanFinished, scan_finished

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


@pytest.fixture
async def watcher(
    tmp_path: Path,
) -> AsyncIterator[tuple[unattended.DirectoryWatcher, RoutedFetcher]]:
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO())
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        yield (
            unattended.DirectoryWatcher(
                tmp_path,
                fetcher,
                journal,
                Releases(FakePoster, fetcher, journal, None),
                store,
                print,
            ),
            fetcher,
        )


@pytest.mark.parametrize(
    ("minutes_to_release", "chrome", "paused"),
    [
        pytest.param(30, False, False, id="open-window-release-in-30-minutes"),
        pytest.param(5, False, True, id="release-in-5-minutes-pauses-research"),
        pytest.param(-40, False, False, id="40-minutes-after-the-rest-is-research-time"),
        pytest.param(30, True, False, id="chrome-window-release-in-30-minutes"),
    ],
)
async def test_research_pauses_only_in_the_minutes_around_a_release(
    watcher: tuple[unattended.DirectoryWatcher, RoutedFetcher],
    minutes_to_release: int,
    chrome: bool,
    paused: bool,
) -> None:
    runner, fetcher = watcher
    expected = runner.journal.now() + timedelta(minutes=minutes_to_release)
    runner.planned = {"a": target("AAA", 1, expected, chrome=chrome)}

    assert runner.busy()
    assert runner.research_paused() is paused
    assert runner.in_window(1) and not runner.in_window(2)
    assert runner.chrome_live() is chrome
    with runner.research_mode():
        assert fetcher.chrome_allowed() is (not chrome and not paused)
        if not paused:
            await fetcher.wait_turn("www.sec.gov")  # SEC still answers research, slowly


async def test_research_has_chrome_again_once_no_window_is_open(
    watcher: tuple[unattended.DirectoryWatcher, RoutedFetcher],
) -> None:
    runner, fetcher = watcher
    expected = runner.journal.now() + timedelta(minutes=30)
    runner.planned = {"a": target("AAA", 1, expected, chrome=True)}
    with runner.research_mode():
        assert not fetcher.chrome_allowed()
    runner.planned = {}
    assert not runner.busy()
    with runner.research_mode():
        assert fetcher.chrome_allowed()


async def test_research_permissions_follow_a_window_that_opens_after_the_task_started(
    watcher: tuple[unattended.DirectoryWatcher, RoutedFetcher],
) -> None:
    runner, fetcher = watcher
    now = runner.journal.now()
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
    # The one-second pause the queue puts between two turns at a host is not what this tests
    monkeypatch.setattr(fetch, "_DEFAULT_GAP", 0.0)
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
        monkeypatch.setattr(dates.changed, "wait", scan_finished)

        # Sea is researched during ONON's window, without Chrome and away from the live host
        with pytest.raises(ScanFinished):
            await dates.run()
        assert checked == [("SE", False, False)]
        assert dates.phases.get(onon.cik) == "Waiting for its own release window to close"

        # In the minutes around a release nothing more is researched at all
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(date_discovery, "chrome_available", lambda: True)
    # A frozen monotonic clock, so the retry time is exact instead of a window around it
    monkeypatch.setattr(date_discovery, "time", SimpleNamespace(monotonic=lambda: 5000.0))
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        # Last investigated months ago, so an investigation is due
        due = company("SE").model_copy(update={"investigated": date(2026, 6, 1)})
        row, _ = calendar.add("SE", due)
        fetcher = RoutedFetcher({})
        dates = DateDiscovery(
            calendar, fetcher, lambda: datetime.now(UTC), lambda: False, lambda _: None
        )
        dates.checked[row.cik] = 5000.0 + 3600
        with fetcher.background(chrome=False):
            await dates.run_once(row)

        # Retried in 10 minutes, not on the next 60-second scan, and not in an hour either
        assert dates.checked[row.cik] == 5000.0 + 10 * 60
        assert dates.phases[row.cik] == "Research yielded to monitoring; retrying in 10 minutes"
        # Nothing was investigated, so the old profile stays
        saved = calendar.find("SE")
        assert saved is not None and saved.company.investigated == date(2026, 6, 1)


@pytest.mark.parametrize(
    ("interruption", "chrome_research"),
    [
        pytest.param("chrome", True, id="chrome-closed-to-research"),
        pytest.param("http", True, id="live-host-busy-with-chrome-research"),
        pytest.param("http", False, id="live-host-busy-without-chrome-research"),
    ],
)
async def test_an_interrupted_investigation_does_not_save_a_partial_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
    chrome_research: bool,
) -> None:
    monkeypatch.setattr(date_discovery, "chrome_available", lambda: chrome_research)
    monkeypatch.setattr(date_discovery, "time", SimpleNamespace(monotonic=lambda: 5000.0))
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
        dates.checked[row.cik] = 5000.0 + 3600

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
            await dates.run_once(row)
        saved = calendar.find("SE")
        assert saved is not None and saved.company.investigated == date(2026, 6, 1)
        assert dates.checked[row.cik] == 5000.0 + 10 * 60


def test_a_window_whose_routes_use_chrome_is_recognized() -> None:
    when = datetime(2026, 11, 17, 7, tzinfo=UTC)
    assert unattended.uses_chrome(target("AAA", 1, when, chrome=True))
    assert not unattended.uses_chrome(target("AAA", 1, when))
