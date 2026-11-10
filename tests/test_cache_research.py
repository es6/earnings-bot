from datetime import datetime
from pathlib import Path

import pytest

from earnings_bot.cache_research import probe_cache
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.recorder import ET, Target
from tests.fakes import RoutedFetcher

URL = "https://issuer.example/press?category=results"


@pytest.mark.parametrize(
    ("normal", "query", "query_status", "query_body", "outcome", "label"),
    [
        ({"x-cache": "TCP_HIT"}, {"x-cache": "TCP_MISS"}, 200, b"feed", (True, False), "verified"),
        ({"age": "10"}, {"age": "1"}, 200, b"feed", (False, False), "Cache hits observed"),
        ({}, {}, 200, b"feed", (False, False), "Freshness unverified"),
        (
            {"cache-control": "public, max-age=0, s-maxage=60"},
            {},
            200,
            b"feed",
            (False, False),
            "Shared TTL 60 s",
        ),
        ({"age": "10"}, {"age": "0"}, 200, b"other", (False, False), "Responses differed"),
        ({}, {}, 403, b"blocked", (False, True), "Both queries rejected"),
    ],
)
async def test_cache_comparison(
    normal: dict[str, str],
    query: dict[str, str],
    query_status: int,
    query_body: bytes,
    outcome: tuple[bool, bool],
    label: str,
) -> None:
    urls: list[str] = []

    async def get(url: str) -> Response:
        urls.append(url)
        if url == URL:
            return Response(url, 200, normal, b"feed", 0)
        return Response(url, query_status, query, query_body, 0)

    result = await probe_cache(get, URL)

    assert (result.query_bust, result.canonical_only) == outcome
    assert label in result.summary
    assert urls[:2] == [URL, URL]
    assert len(set(urls[2:])) == 2
    assert all(url.startswith(f"{URL}&_=") for url in urls[2:])


async def test_a_feed_that_rejects_query_strings_keeps_its_readable_url(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({f"{URL}&_=": (403, b"blocked"), URL: (200, b"feed")})
    now = datetime(2026, 11, 10, 3, tzinfo=ET)
    with DeliveryStore(tmp_path / "calendar.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("AAPL", 1, now)
        calendar.set_target(Target(ticker=row.ticker, cik=row.cik, expected=now, rss=[URL]), "Test")
        [planned] = calendar.entries()
        dates = DateDiscovery(calendar, fetcher, lambda: now, lambda: False, lambda _: None)
        await dates._prepare_cache(planned)
        [saved] = calendar.entries()

    assert saved.target and saved.target.rss_canonical == [URL]
    [feed] = [source for source in saved.target.sources() if source.name.startswith("rss:")]
    assert feed.url() == URL
