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
WERIDE = {
    "cf-cache-status": "DYNAMIC",
    "cache-control": "public, max-age=0, s-maxage=2592000",
    "last-modified": "Mon, 09 Nov 2026 05:55:28 GMT",
    "x-cache-hits": "372",
}


@pytest.mark.parametrize(
    ("normal", "query", "query_status", "query_body", "outcome", "label"),
    [
        (
            {"x-cache": "TCP_HIT"},
            {"x-cache": "TCP_MISS"},
            200,
            b"feed",
            (True, False, False),
            "Query bypass verified",
        ),
        (
            {"cache-status": '"Cloudflare"; hit'},
            {"cache-status": '"Cloudflare"; fwd=uri-miss'},
            200,
            b"feed",
            (True, False, False),
            "Query bypass verified",
        ),
        ({"age": "10"}, {"age": "1"}, 200, b"feed", (False, False, True), "don't get past"),
        # Cloudflare passes the request through to the IR platform's own cache, which serves
        # its stored copy to every URL variant (ir.weride.ai, Nov 10)
        (
            WERIDE,
            WERIDE,
            200,
            b"feed",
            (False, False, True),
            "x-cache-hits: 372, last rebuilt Mon, Nov 9 at 12:55 AM ET",
        ),
        (
            {"x-varnish": "32770"},
            {"x-varnish": "98306 32770"},
            200,
            b"feed",
            (False, False, True),
            "don't get past",
        ),
        (
            {"cf-cache-status": "DYNAMIC"},
            {"cf-cache-status": "DYNAMIC"},
            200,
            b"feed",
            (False, False, False),
            "Not cached",
        ),
        ({}, {}, 200, b"feed", (False, False, False), "Freshness unverified"),
        (
            {"cache-control": "public, max-age=0, s-maxage=60"},
            {},
            200,
            b"feed",
            (False, False, False),
            "Shared TTL 60 s",
        ),
        ({"age": "10"}, {"age": "0"}, 200, b"other", (False, False, False), "Responses differed"),
        ({}, {}, 403, b"blocked", (False, True, False), "Both queries rejected"),
    ],
)
async def test_cache_comparison(
    normal: dict[str, str],
    query: dict[str, str],
    query_status: int,
    query_body: bytes,
    outcome: tuple[bool, bool, bool],
    label: str,
) -> None:
    urls: list[str] = []

    async def get(url: str) -> Response:
        urls.append(url)
        if url == URL:
            return Response(url, 200, normal, b"feed", 0)
        return Response(url, query_status, query, query_body, 0)

    result = await probe_cache(get, URL)

    assert (result.query_bust, result.canonical_only, result.stored_only) == outcome
    assert label in result.summary
    assert urls[:2] == [URL, URL]
    assert len(set(urls[2:])) == 2
    assert all(url.startswith(f"{URL}&_=") for url in urls[2:])


async def test_a_feed_that_rejects_query_strings_keeps_its_readable_url(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({f"{URL}&_=": (403, b"blocked"), URL: (200, b"feed")})
    now = datetime(2026, 11, 10, 3, tzinfo=ET)
    with DeliveryStore(tmp_path / "calendar.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("AAPL")
        calendar.set_target(Target(ticker=row.ticker, cik=row.cik, expected=now, rss=[URL]), "Test")
        [planned] = calendar.entries()
        dates = DateDiscovery(calendar, fetcher, lambda: now, lambda: False, lambda _: None)
        await dates._prepare_cache(planned)
        [saved] = calendar.entries()

    assert saved.target and saved.target.rss_canonical == [URL]
    [feed] = [source for source in saved.target.sources() if source.name.startswith("rss:")]
    assert feed.url() == URL


@pytest.mark.parametrize("reported", [False, True], ids=["no-telemetry", "cache-miss"])
async def test_dynamic_html_doesnt_hide_the_actual_cache_finding(reported: bool) -> None:
    calls = 0

    async def get(url: str) -> Response:
        nonlocal calls
        calls += 1
        # ROAD's listing includes changing page metadata, while its release list is stable.
        body = (
            f'<title>Issuer press releases</title><input name="csrf" value="{calls}">'
            '<a href="/release/q3">Third Quarter 2026 Results</a>'
        ).encode()
        headers = {"x-cache": "HIT" if url == URL else "MISS"} if reported else {}
        return Response(url, 200, headers, body, 0)

    result = await probe_cache(get, URL)

    assert result.query_bust is reported
    assert not result.stored_only
    assert "Responses differed" not in result.summary
    assert ("Query bypass verified" if reported else "No cache reported a hit or a miss") in (
        result.summary
    )


async def test_a_different_page_title_cannot_verify_a_query_bypass() -> None:
    async def get(url: str) -> Response:
        title = "Issuer press releases" if url == URL else "Sign in"
        return Response(
            url,
            200,
            {"x-cache": "HIT" if url == URL else "MISS"},
            f"<title>{title}</title>".encode(),
            0,
        )

    result = await probe_cache(get, URL)

    assert not result.query_bust
    assert "Responses differed" in result.summary
