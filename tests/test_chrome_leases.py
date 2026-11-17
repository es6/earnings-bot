"""The shared desktop Chrome stays open while anyone is using it. A scan used to close it
whenever no open window needed it, under a source audit's request (AMAT, Nov 15; KLAR, Nov 17:
`assert self._session is not None`) and while closing tabs (Nov 16: "dictionary changed size
during iteration"). Now each user holds a lease, and a scan closes Chrome only when none does."""

import asyncio
import io
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from first_print import fetch, unattended
from first_print.browser import DesktopChrome
from first_print.calendar import Calendar
from first_print.date_discovery import DateDiscovery
from first_print.delivery import DeliveryStore
from first_print.fetch import Fetcher, Response
from first_print.pipeline import Release, Releases
from first_print.publication_trials import run_trials
from first_print.recorder import Journal, Watch
from first_print.source_audit import audit_sources
from first_print.sources import RssFeed, Sighting
from first_print.unattended import DirectoryWatcher
from tests.builders import AMAT, events, plan, uber
from tests.fakes import FakePoster, QueuedFetcher, RoutedFetcher


@pytest.fixture(autouse=True)
def quick_host_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_DEFAULT_GAP", 0.02)


class SharedChrome:
    """Stands in for the shared DesktopChrome. A request can be held open, and one still
    running when Chrome closes fails the way the real one did."""

    def __init__(self) -> None:
        self.closed = False
        self.asked = asyncio.Event()
        self.go = asyncio.Event()
        self.go.set()

    async def request(self, url: str, *args: Any, **kwargs: Any) -> Response:
        self.asked.set()
        await self.go.wait()
        assert not self.closed, "Chrome closed under a request"
        return Response(url, 200, {}, b"", 0.01)

    async def close_observations(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def shared_chrome(fetcher: Fetcher) -> SharedChrome:
    chrome = SharedChrome()
    fetcher._browser = cast(DesktopChrome, chrome)
    return chrome


class HeldFetcher(RoutedFetcher):
    """Holds every HTTP request until let go, so a test can scan in the middle of some work."""

    def __init__(self) -> None:
        super().__init__({})
        self.asked = asyncio.Event()
        self.go = asyncio.Event()

    async def request(self, *args: Any, **kwargs: Any) -> Response:
        self.asked.set()
        await self.go.wait()
        return await super().request(*args, **kwargs)


@contextmanager
def watcher_for(tmp_path: Path, fetcher: Fetcher, log: io.StringIO) -> Iterator[DirectoryWatcher]:
    """The bot's watcher. With no schedule files, no window needs Chrome."""
    journal = Journal(log)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        yield DirectoryWatcher(
            tmp_path, fetcher, journal, Releases(FakePoster, fetcher, journal, None), store, print
        )


async def stop(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_scan_leaves_chrome_open_while_research_holds_a_lease(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({})
    chrome = shared_chrome(fetcher)
    finished = asyncio.Event()

    async def research() -> None:
        with fetcher.browser_lease("HD research"):
            await finished.wait()

    log = io.StringIO()
    with watcher_for(tmp_path, fetcher, log) as watcher:
        task = asyncio.create_task(research())
        await asyncio.sleep(0)
        await watcher.scan()
        await watcher.scan()
        assert not chrome.closed
        # Logged once while the same holders keep it open
        assert [e["holders"] for e in events(log, "browser_close_deferred")] == [["HD research"]]

        finished.set()
        await task
        await watcher.scan()
    assert chrome.closed
    assert fetcher.chromes() == []


async def test_a_lease_released_after_a_window_starts_needing_chrome_leaves_it_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def watch(*args: object, **kwargs: object) -> list[Watch]:
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(unattended, "record_target", watch)
    fetcher = RoutedFetcher({})
    chrome = shared_chrome(fetcher)
    with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
        with fetcher.browser_lease("HD research"):
            await watcher.scan()  # no window yet: only the lease keeps Chrome open
            when = datetime.now(UTC) + timedelta(minutes=5)
            (tmp_path / "a.toml").write_text(
                f'[[target]]\nticker = "AAA"\ncik = 1\nexpected = {when.isoformat()}\n'
                '[target.ir]\nlisting = "https://ir.example.com/news"\n'
                'headline = "third quarter"\n',
                encoding="utf-8",
            )
        await watcher.scan()  # the lease is gone, but the window now watches an IR page
        try:
            assert list(watcher.windows)
            assert not chrome.closed
        finally:
            for window in watcher.windows.values():
                await stop(window.task)


async def test_a_cancelled_lease_holder_gives_its_lease_back(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({})
    chrome = shared_chrome(fetcher)

    async def research() -> None:
        with fetcher.browser_lease("HD research"):
            await asyncio.Event().wait()

    with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
        task = asyncio.create_task(research())
        await asyncio.sleep(0)
        await watcher.scan()
        assert not chrome.closed

        await stop(task)
        assert fetcher.browser_holders() == []
        await watcher.scan()
    assert chrome.closed


async def test_a_browser_request_in_flight_survives_a_scan(tmp_path: Path) -> None:
    async with RoutedFetcher({}) as fetcher:
        chrome = shared_chrome(fetcher)
        chrome.go.clear()
        with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
            url = "https://ir.example.com/news"
            request = asyncio.create_task(fetcher.browser_request(url, url, 8))
            await asyncio.wait_for(chrome.asked.wait(), 5)
            await watcher.scan()
            assert not chrome.closed

            chrome.go.set()
            assert (await request).status == 200
            await watcher.scan()
        assert chrome.closed


async def test_chrome_survives_a_scan_during_a_source_audit(tmp_path: Path) -> None:
    fetcher = HeldFetcher()
    chrome = shared_chrome(fetcher)
    with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
        now = datetime.now(UTC)
        audit = asyncio.create_task(audit_sources(fetcher, tmp_path, "AMAT", AMAT, now, None))
        await asyncio.wait_for(fetcher.asked.wait(), 5)
        await watcher.scan()
        assert not chrome.closed

        await stop(audit)
        await watcher.scan()
    assert chrome.closed


async def test_chrome_survives_a_scan_during_track_research(tmp_path: Path) -> None:
    fetcher = HeldFetcher()
    chrome = shared_chrome(fetcher)
    with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
        calendar = Calendar(watcher.store)
        row, _ = calendar.add("NVDA")
        dates = DateDiscovery(calendar, fetcher, lambda: datetime.now(UTC), lambda: False, print)
        research = asyncio.create_task(dates.run_once(row))
        await asyncio.wait_for(fetcher.asked.wait(), 5)
        await watcher.scan()
        assert not chrome.closed

        await stop(research)
        await watcher.scan()
    assert chrome.closed


async def test_chrome_survives_a_scan_while_a_release_is_read(tmp_path: Path) -> None:
    fetcher = HeldFetcher()
    chrome = shared_chrome(fetcher)
    with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
        release = Release(uber(), FakePoster(), fetcher, Journal(io.StringIO()), None)
        sighting = Sighting(
            "one", "Quarterly results", "https://issuer.example/earnings", earnings=True
        )
        release.add(RssFeed("issuer", "https://issuer.example/rss"), sighting)
        await asyncio.wait_for(fetcher.asked.wait(), 5)
        await watcher.scan()
        assert not chrome.closed

        for task in list(release.tasks):
            await stop(task)
        await watcher.scan()
    assert chrome.closed


async def test_chrome_survives_a_scan_between_a_chrome_trial_s_polls(tmp_path: Path) -> None:
    chrome_route = {
        "name": "listing",
        "url": "https://ir.example.com/news",
        "warmup": "https://ir.example.com/",
        "transport": "chrome",
        "interval": 30,
        "issuer_pattern": "Example Corp",
        "period_pattern": "third quarter",
        "result_pattern": "results",
    }
    http_route = chrome_route | {"name": "article", "transport": "http", "warmup": None}
    trial_plan = plan(routes=[chrome_route, http_route], duration_seconds=60, max_requests=10)
    async with QueuedFetcher({"example.com": [b"Older news"]}) as fetcher:
        chrome = shared_chrome(fetcher)
        with watcher_for(tmp_path, fetcher, io.StringIO()) as watcher:
            with closing(sqlite3.connect(":memory:")) as db:
                trial = asyncio.create_task(
                    run_trials(trial_plan, fetcher, Journal(io.StringIO()), db)
                )
                await asyncio.wait_for(chrome.asked.wait(), 5)
                await asyncio.sleep(0.1)  # the poll is answered; the route waits for its next
                await watcher.scan()
                assert not chrome.closed

                await stop(trial)
            await watcher.scan()
        assert chrome.closed
