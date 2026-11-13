"""Replay the ONON/Sea source-plan failures with reduced real public responses.

These fixtures were captured after the incidents. They prove planning and matching,
not the exact historical time the endpoints first exposed the release.
"""

import io
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from earnings_bot.browser_sources import IRSource
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery, _cache_routes, parse_notice
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.investigate import INVESTIGATION
from earnings_bot.ir_research import quarter_headline
from earnings_bot.issuer_news import IssuerNews, news_records
from earnings_bot.known import KnownCompany, SourceCheck
from earnings_bot.pipeline import describe_source
from earnings_bot.recorder import ET, Journal, Target, Watch, poll, take
from earnings_bot.source_audit import Speed, Timing
from earnings_bot.sources import Q4PressReleases, Sighting
from tests.fakes import RoutedFetcher

FIXTURES = json.loads((Path(__file__).parent / "fixtures/ir_misses.json").read_text())
RESULTS = "https://www.sea.com/api/financial/resource"
NEWS = "https://www.sea.com/api/invest/news?page=1&size=12"
NOW = datetime(2026, 11, 9, 12, tzinfo=ET)


def company(ticker: str) -> KnownCompany:
    return KnownCompany(
        name="Sea Limited" if ticker == "SE" else "On Holding AG",
        cik=1703399 if ticker == "SE" else 1858985,
        platform="own-site",
        reachable=False,
        wire="unknown",
        sec_form="6-K",
        release_time="06:10",
        fastest="unknown",
        evidence="Saved public source replay",
        checked=NOW.date(),
        investigated=NOW.date(),
        investigation=INVESTIGATION,
        checks=[SourceCheck(source="SEC", watch=True, why="Verified 6-K filer")],
        ir_news_api=NEWS if ticker == "SE" else None,
        ir_results_api=RESULTS if ticker == "SE" else None,
    )


@pytest.mark.parametrize("legacy_results_alias", [False, True])
async def test_track_keeps_the_results_api_when_the_newsroom_omits_the_release(
    tmp_path: Path, legacy_results_alias: bool
) -> None:
    records = news_records(RESULTS, json.dumps(FIXTURES["sea_results"]).encode())
    routes = {
        RESULTS: (200, json.dumps(FIXTURES["sea_results"]).encode()),
        NEWS: (200, json.dumps(FIXTURES["sea_news"]).encode()),
        **{row.url: (200, b"%PDF-1.7\nfixture") for row in records},
    }
    fetcher = RoutedFetcher(routes)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("SE", company("SE"))
        planned = Target(
            ticker="SE",
            cik=row.cik,
            expected=NOW + timedelta(days=1),
            period="FY2026Q3",
            sec_form="6-K",
            ir=IRSource(
                news_api=RESULTS if legacy_results_alias else NEWS,
                headline=quarter_headline(2026, 3),
                cache_bust_urls=[RESULTS if legacy_results_alias else NEWS],
            ),
        )
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, lambda _: None)
        prepared = await dates._issuer_routes(row, planned, "Issuer date confirmed")
        assert planned.ir and prepared.ir and prepared.ir.results_api == RESULTS
        assert prepared.ir.cache_bust_urls == planned.ir.cache_bust_urls
        sources = [s for s in prepared.sources() if isinstance(s, IssuerNews)]
        assert len({s.api for s in sources}) == len(sources)
        result_source = next(s for s in sources if s.name == "issuer-results")
        if not legacy_results_alias:
            assert prepared.ir.news_api == NEWS
            newsroom = next(s for s in sources if s.name == "issuer-news")
            assert (await poll(newsroom, fetcher))[1] == []
        response, found = await poll(result_source, fetcher)
        assert len(found) == 1 and found[0].body == response.body
        assert "3Q2026" in found[0].url
        assert describe_source(result_source) == "IR site"
        calendar.set_target(prepared, "Issuer date confirmed")
        [saved] = calendar.entries()
        urls = {route.url for route in _cache_routes(saved)}
        assert RESULTS in urls
        if not legacy_results_alias:
            assert NEWS in urls


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


async def test_a_new_publication_audit_replans_before_the_hourly_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ScanFinished(Exception):
        pass

    async def finished() -> None:
        raise ScanFinished

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
        monkeypatch.setattr(dates.changed, "wait", finished)
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

    class ScanFinished(Exception):
        pass

    async def finished() -> None:
        raise ScanFinished

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
        monkeypatch.setattr(dates.changed, "wait", finished)
        for _ in range(3):
            with pytest.raises(ScanFinished):
                await dates.run()
        assert checks == 1
