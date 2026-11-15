"""Replay the ONON/Sea source-plan failures with reduced real public responses.

These fixtures were captured after the incidents. They prove planning and matching,
not the exact historical time the endpoints first exposed the release.
"""

import io
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from earnings_bot.browser_sources import IRSource
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery, _cache_routes, parse_notice
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.ir_research import quarter_headline
from earnings_bot.issuer_news import IssuerNews, news_records
from earnings_bot.pipeline import describe_source
from earnings_bot.recorder import ET, Journal, Target, Watch, poll, take
from earnings_bot.source_audit import Speed, Timing
from earnings_bot.sources import Q4PressReleases, Sighting
from tests.builders import IR_COVERAGE_NOW as NOW
from tests.builders import IR_COVERAGE_RESULTS as RESULTS
from tests.builders import NEWS, company
from tests.fakes import RoutedFetcher, ScanFinished, scan_finished

FIXTURES = json.loads((Path(__file__).parent / "fixtures/ir_misses.json").read_text())


@dataclass
class Planned:
    """A Sea target prepared by `_issuer_routes`, with what polls it and the saved calendar."""

    calendar: Calendar
    fetcher: RoutedFetcher
    planned: Target
    prepared: Target

    @property
    def sources(self) -> dict[str, IssuerNews]:
        return {s.name: s for s in self.prepared.sources() if isinstance(s, IssuerNews)}


@pytest.fixture(params=["newsroom-api", "legacy-results-alias"])
async def sea_plan(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Planned]:
    api = RESULTS if request.param == "legacy-results-alias" else NEWS
    records = news_records(RESULTS, json.dumps(FIXTURES["sea_results"]).encode())
    fetcher = RoutedFetcher(
        {
            RESULTS: (200, json.dumps(FIXTURES["sea_results"]).encode()),
            NEWS: (200, json.dumps(FIXTURES["sea_news"]).encode()),
            **{row.url: (200, b"%PDF-1.7\nfixture") for row in records},
        }
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("SE", company("SE"))
        planned = Target(
            ticker="SE",
            cik=row.cik,
            expected=NOW + timedelta(days=1),
            period="FY2026Q3",
            sec_form="6-K",
            ir=IRSource(news_api=api, headline=quarter_headline(2026, 3), cache_bust_urls=[api]),
        )
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, lambda _: None)
        prepared = await dates._issuer_routes(row, planned, "Issuer date confirmed")
        yield Planned(calendar, fetcher, planned, prepared)


def test_the_plan_keeps_the_results_api_and_one_source_per_api(sea_plan: Planned) -> None:
    assert sea_plan.planned.ir and sea_plan.prepared.ir
    assert sea_plan.prepared.ir.results_api == RESULTS
    assert sea_plan.prepared.ir.cache_bust_urls == sea_plan.planned.ir.cache_bust_urls
    sources = list(sea_plan.sources.values())
    assert len({s.api for s in sources}) == len(sources)
    assert describe_source(sea_plan.sources["issuer-results"]) == "IR site"


async def test_the_results_api_finds_the_release_the_newsroom_omits(sea_plan: Planned) -> None:
    response, found = await poll(sea_plan.sources["issuer-results"], sea_plan.fetcher)

    assert len(found) == 1 and found[0].body == response.body
    assert "3Q2026" in found[0].url


@pytest.mark.parametrize("sea_plan", ["newsroom-api"], indirect=True)
async def test_a_newsroom_api_stays_planned_but_lists_no_release(sea_plan: Planned) -> None:
    assert sea_plan.prepared.ir and sea_plan.prepared.ir.news_api == NEWS
    _, found = await poll(sea_plan.sources["issuer-news"], sea_plan.fetcher)
    assert found == []


@pytest.mark.parametrize(
    ("sea_plan", "cached"),
    [
        pytest.param("newsroom-api", {RESULTS, NEWS}, id="newsroom-api"),
        pytest.param("legacy-results-alias", {RESULTS}, id="legacy-results-alias"),
    ],
    indirect=["sea_plan"],
)
def test_the_saved_plan_checks_the_cache_of_each_route_it_polls(
    sea_plan: Planned, cached: set[str]
) -> None:
    sea_plan.calendar.set_target(sea_plan.prepared, "Issuer date confirmed")
    [saved] = sea_plan.calendar.entries()
    assert cached <= {route.url for route in _cache_routes(saved)}


@pytest.mark.parametrize("api", ["https://127.0.0.1/api", "http://www.sea.com/api"])
def test_results_api_preserves_the_public_https_boundary(api: str) -> None:
    with pytest.raises(ValidationError):
        IRSource(results_api=api, headline=quarter_headline(2026, 3))


def test_the_saved_on_release_is_caught_up_on_the_first_q4_poll() -> None:
    source = Q4PressReleases("investors.on-running.com")
    start = datetime(2026, 11, 10, 5, 10, tzinfo=ET)
    watched = Watch("ONON", source, window=(start, start + timedelta(hours=3)), period="FY2026Q3")
    seen: list[Sighting] = []
    response = Response(source.url(), 200, {}, json.dumps(FIXTURES["onon"]).encode(), 0.01)
    journal = Journal(io.StringIO())
    take(watched, start, response, journal, lambda _, item: seen.append(item))
    take(
        watched, start + timedelta(seconds=1), response, journal, lambda _, item: seen.append(item)
    )
    assert len(seen) == 1 and seen[0].key == "1791"
    assert seen[0].caught_up and seen[0].published == "11/10/2026 05:00:00"
    assert watched.first is None  # a catch-up is not a measured detection latency


@pytest.mark.parametrize(
    ("window_opens", "published", "caught_up"),
    [
        pytest.param(9, "2026-11-10T05:00:00-05:00", True, id="same-day-hours-before"),
        pytest.param(9, "2026-11-10T10:00:00-05:00", True, id="inside-the-window"),
        pytest.param(14, "2026-11-10T01:00:00-05:00", False, id="over-12-hours-before"),
        pytest.param(5, "2026-11-09T23:00:00-05:00", False, id="the-evening-before"),
        pytest.param(9, "2026-11-10T13:00:00-05:00", False, id="after-the-window-closed"),
    ],
)
def test_an_early_release_with_a_clock_time_is_caught_up_only_inside_its_day_and_12_hours(
    window_opens: int, published: str, caught_up: bool
) -> None:
    source = Q4PressReleases("investors.on-running.com")
    start = datetime(2026, 11, 10, window_opens, 0, tzinfo=ET)
    watched = Watch("ONON", source, window=(start, start + timedelta(hours=3)), period="FY2026Q3")
    body = json.loads(json.dumps(FIXTURES["onon"]))
    body["GetPressReleaseListResult"][0]["PressReleaseDate"] = published
    response = Response(source.url(), 200, {}, json.dumps(body).encode(), 0.01)
    seen: list[Sighting] = []

    take(watched, start, response, Journal(io.StringIO()), lambda _, item: seen.append(item))
    take(
        watched,
        start + timedelta(seconds=1),
        response,
        Journal(io.StringIO()),
        lambda _, item: seen.append(item),
    )

    assert [(item.key, item.caught_up) for item in seen] == ([("1791", True)] if caught_up else [])


async def test_a_new_publication_audit_replans_before_the_hourly_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("ONON", company("ONON"))
        dates = DateDiscovery(
            calendar, RoutedFetcher({}), lambda: NOW, lambda: False, lambda _: None
        )
        reads = 0

        async def notice(row, now, unread):
            nonlocal reads
            reads += 1
            return parse_notice(
                "ONON",
                row.company,
                "On to Release Third Quarter 2026 Results on Tuesday, November 10, 2026",
                "On will report before the U.S. market opens on November 10, 2026.",
                now,
            )

        monkeypatch.setattr(dates, "_issuer_date", notice)
        await dates._check(row)
        first = calendar.entries()[0].target
        assert first and first.expected.hour == 6
        dates.checked[row.cik] = time.monotonic() + 3600
        speed = Speed(
            date(2026, 8, 11),
            confident=True,
            form="6-K",
            archive=[
                Timing("q4_press", datetime(2026, 8, 11, 5, tzinfo=ET)),
                Timing("SEC 6-K acceptance", datetime(2026, 8, 11, 6, 10, tzinfo=ET)),
            ],
        )
        calendar.save_review(row.cik, NOW, speed.to_json())
        monkeypatch.setattr(dates.changed, "wait", scan_finished)
        with pytest.raises(ScanFinished):
            await dates.run()
        prepared = calendar.entries()[0].target
        assert prepared and (prepared.expected.hour, prepared.expected.minute) == (5, 0)
        assert prepared.window[0] < datetime(2026, 11, 10, 5, tzinfo=ET)
        assert reads == 2
        # The unchanged audit does not repeatedly bypass the hourly cadence.
        with pytest.raises(ScanFinished):
            await dates.run()
        assert reads == 2


async def test_failed_research_waits_an_hour_even_with_a_saved_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that fails before it reads the audit must not be retried on every scan: that
    would ask the same IR sites for research once a minute instead of once an hour."""
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("ONON", company("ONON"))
        speed = Speed(
            date(2026, 8, 11),
            confident=True,
            form="6-K",
            archive=[Timing("q4_press", datetime(2026, 8, 11, 5, tzinfo=ET))],
        )
        calendar.save_review(row.cik, NOW, speed.to_json())
        dates = DateDiscovery(
            calendar, RoutedFetcher({}), lambda: NOW, lambda: False, lambda _: None
        )
        checks = 0

        async def failing(row, **_: object) -> None:
            nonlocal checks
            checks += 1
            raise RuntimeError("Chrome went away")

        monkeypatch.setattr(dates, "_check", failing)
        monkeypatch.setattr(dates.changed, "wait", scan_finished)
        for _ in range(3):
            with pytest.raises(ScanFinished):
                await dates.run()
        assert checks == 1
