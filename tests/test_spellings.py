"""Polling a feed under a new spelling of its path, for caches that ignore query strings."""

import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import pytest
from pydantic import ValidationError

from earnings_bot.browser_sources import BrowserRelease, BrowserRss, IRSource
from earnings_bot.cache_research import probe_cache
from earnings_bot.calendar import Calendar
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.issuer_news import IssuerNews
from earnings_bot.known import target_toml
from earnings_bot.recorder import ET, Plan, Target, Trial
from earnings_bot.sources import RssFeed, Spellings
from tests.fakes import FakeSession, SessionFetcher

FEED = "https://ir.hubspot.com/rss/news-releases.xml"
LISTING = "https://nebius.com/newsroom"
TITLE = "<title>HubSpot, Inc. - News Releases</title>"
STORED = {
    "cf-cache-status": "DYNAMIC",
    "last-modified": "Mon, 09 Nov 2026 18:55:39 GMT",
    "x-cache-hits": "657",
}
# What HubSpot's feed answered a new spelling with on Nov 10, 2026: built on request
BUILT_NOW = {"cf-cache-status": "DYNAMIC", "last-modified": "Tue, 10 Nov 2026 21:10:07 GMT"}


def feed(*items: str) -> bytes:
    entries = "".join(f"<item><title>{item}</title><guid>{item}</guid></item>" for item in items)
    return f"<rss><channel>{TITLE}{entries}</channel></rss>".encode()


def test_spellings_dont_repeat_and_name_the_same_url() -> None:
    spellings = Spellings(FEED)

    urls = [spellings.next() for _ in range(3000)]

    assert len(set(urls)) == 3000
    assert all(unquote(url) == FEED for url in urls)
    # The first folder stays as written, since some sites route on it
    assert all(url.startswith("https://ir.hubspot.com/rss/") for url in urls)
    # Once most spellings with two encoded letters are used, more letters are encoded
    assert min(url.count("%") for url in urls) == 2 < max(url.count("%") for url in urls)


def test_a_short_path_can_be_spelled_anywhere() -> None:
    # Every letter of "/newsroom": the rest of the path would have too few to spell
    assert Spellings(LISTING).positions == list(range(1, len("/newsroom")))


async def test_a_spelled_feed_goes_out_as_written_and_parses_like_the_plain_one() -> None:
    async def answer(method: str, url: str) -> tuple[int, dict[str, str], bytes]:
        return 200, BUILT_NOW, feed("HubSpot Reports Q3 2026 Results")

    session = FakeSession(answer)
    source = RssFeed("rss:ir.hubspot.com", FEED, ["HubSpot"], spell=True)
    async with SessionFetcher(session) as fetcher:
        fetched = await source.fetch(fetcher)

    # The fetcher would otherwise decode %6E back to "n" and ask for the cached URL
    assert session.sent[0][2] == fetched.url != FEED
    assert unquote(fetched.url) == FEED
    [item] = source.parse(fetched)
    assert item.earnings
    # Its polls aren't shared with a plain or query-busted copy of the feed
    assert source.share_key != RssFeed("rss:ir.hubspot.com", FEED, bust_cache=True).share_key


def test_ir_sources_can_be_spelled() -> None:
    api = "https://ir.example.com/api/v1/press-releases/list"
    rss = "https://ir.example.com/rss/news-releases.xml"
    ir = IRSource(
        listing=LISTING,
        news_api=api,
        headline="(?i).*third quarter 2026.*results.*",
        spelled_urls=[LISTING, api],
    )
    listing, news = ir.sources()[:2]
    assert isinstance(listing, BrowserRelease) and isinstance(news, IssuerNews)
    assert unquote(listing.listing_url()) == LISTING != listing.listing_url()
    assert unquote(news.url()) == api != news.url()

    browser_feed = BrowserRss(rss, "https://ir.example.com/", ".*", spell=True)
    assert unquote(browser_feed.url()) == rss != browser_feed.url()

    with pytest.raises(ValidationError, match="spelled URL"):
        IRSource(listing=LISTING, headline=".*", spelled_urls=["https://elsewhere.example/feed"])


def test_a_schedule_names_its_spelled_feeds_and_they_poll_spelled() -> None:
    target = Target(
        ticker="HUBS",
        cik=1404655,
        expected=datetime(2027, 2, 11, 16, 5, tzinfo=ET),
        rss_spelled=[FEED],
        trial=Trial(rss=[FEED]),
    )

    [plan] = Plan.model_validate(tomllib.loads(target_toml(target))).target

    assert plan.rss_spelled == [FEED]
    [source] = [s for s in plan.sources() if s.name.startswith("rss:")]
    assert isinstance(source, RssFeed)
    assert unquote(source.url()) == FEED != source.url()


def routes(spelled: tuple[int, dict[str, str], bytes]):
    async def get(url: str) -> Response:
        if url == FEED or url.startswith(f"{FEED}?_="):
            return Response(url, 200, STORED, feed("Q2 2026 Results"), 0)
        status, headers, body = spelled
        return Response(url, status, headers, body, 0)

    return get


@pytest.mark.parametrize(
    ("spelled", "outcome"),
    [
        # HubSpot: a fresh copy, which can already list the new release
        ((200, BUILT_NOW, feed("Q3 2026 Results", "Q2 2026 Results")), True),
        # WeRide answers 404 to every encoded spelling
        ((404, {}, b"Not found"), False),
        # A spelling that gets a stored copy of its own doesn't help
        ((200, STORED, feed("Q2 2026 Results")), False),
        # However fresh, a different page isn't the feed
        ((200, BUILT_NOW, b"<html><title>Page not found</title></html>"), False),
    ],
)
async def test_a_new_spelling_can_get_past_a_cache_that_ignores_queries(
    spelled: tuple[int, dict[str, str], bytes], outcome: bool
) -> None:
    result = await probe_cache(routes(spelled), FEED)

    assert result.spelled is outcome
    assert result.stored_only is not outcome
    assert ("reaches the server" in result.summary) is outcome


class SpellingFetcher:
    """Answers the cache check the way HubSpot's feed did on Nov 10, 2026."""

    def __init__(self) -> None:
        self.get = routes((200, BUILT_NOW, feed("Q2 2026 Results")))

    async def request(self, method: str, url: str, *args: object, **kwargs: object) -> Response:
        return await self.get(url)


async def test_the_planner_polls_a_feed_by_new_spellings_once_the_check_finds_they_work(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 10, 18, 30, tzinfo=ET)
    with DeliveryStore(tmp_path / "calendar.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("HUBS")
        target = Target(ticker=row.ticker, cik=row.cik, expected=now, rss=[FEED])
        calendar.set_target(target, "Test")
        [planned] = calendar.entries()
        fetcher = SpellingFetcher()
        dates = DateDiscovery(calendar, fetcher, lambda: now, lambda: False, lambda _: None)  # type: ignore[arg-type]
        await dates._prepare_cache(planned)
        [saved] = calendar.entries()

    assert saved.target and saved.target.rss_spelled == [FEED]
    [source] = [s for s in saved.target.sources() if s.name.startswith("rss:")]
    assert unquote(source.url()) == FEED != source.url()
    assert "?_=" not in source.url()
