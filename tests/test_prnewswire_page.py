"""PR Newswire's earnings listing page as a live route beside its earnings feed.

The fixtures are the page as PR Newswire served it on Nov 19, 2026 at 06:10 UTC: the plain URL
(25 rows) and, a few seconds later, the same listing asked for with pagesize=41."""

import asyncio
import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from first_print.fetch import Response, host_gap
from first_print.recorder import ET, Journal, Polls, Target, Watch, watch
from first_print.sources import (
    PR_NEWSWIRE_EARNINGS,
    PR_NEWSWIRE_EARNINGS_PAGE,
    FreshPrNewswire,
    PrNewswirePage,
    RssFeed,
    Sighting,
    Source,
    looks_like_earnings,
)
from tests.fakes import FakeFetcher, RoutedFetcher, SessionFetcher
from tests.test_sources import RedirectRecordingSession

FIXTURES = Path(__file__).parent / "fixtures" / "prnewswire"
PAGE = (FIXTURES / "earnings-list.html").read_bytes()
LONGER_PAGE = (FIXTURES / "earnings-list-pagesize-41.html").read_bytes()
ANSWERED = {"date": "Thu, 19 Nov 2026 06:10:10 GMT"}
ARTICLE = re.compile(r"https://www\.prnewswire\.com/news-releases/[^/?#]+-\d{9}\.html")
PANW = "palo-alto-networks-reports-fiscal-first-quarter-2027-financial-results-302944393.html"


def page(body: bytes, headers: dict[str, str] | None = None) -> Response:
    return Response(PR_NEWSWIRE_EARNINGS_PAGE, 200, headers or ANSWERED, body, 0.4)


def without_row(body: bytes, slug: str) -> bytes:
    """The page with one release's row taken out."""
    row = re.compile(
        rb'<a class="newsreleaseconsolidatelink[^>]*href="/news-releases/'
        + re.escape(slug.encode())
        + rb'".*?</a>',
        re.DOTALL,
    )
    trimmed, count = row.subn(b"", body)
    assert count == 1
    return trimmed


def test_the_earnings_page_lists_every_release_row_of_a_saved_page() -> None:
    sightings = PrNewswirePage("prnewswire_html").parse(page(PAGE))

    assert len(sightings) == 25
    assert len({s.key for s in sightings}) == 25
    assert all(ARTICLE.fullmatch(s.url) and s.key == s.url for s in sightings)
    first = sightings[0]
    assert first.url == (
        "https://www.prnewswire.com/news-releases/joyy-reports-third-quarter-2026-financial-"
        "results-total-revenues-record-consistent-increases-driven-by-sustained-growth-of-three"
        "-core-businesses-302944743.html"
    )
    assert first.title == (
        "JOYY Reports Third Quarter 2026 Financial Results: Total revenues record consistent "
        "increases, driven by sustained growth of three core businesses"
    )
    # The row's minute stamp, as a time with its offset, and as PR Newswire wrote it
    assert first.published == "2026-11-18T22:50:00-05:00"
    assert first.details == {"listed": "Nov 18, 2026, 22:50 ET"}
    # Entities decoded, tags gone: the lead reads like the feed's description
    assert first.lead.startswith('JOYY Inc. (NASDAQ: JOYY) ("JOYY" or the "Company")')
    assert [s.earnings for s in sightings] == [looks_like_earnings(s.title) for s in sightings]
    assert sightings[-1].published == "2026-11-17T08:35:00-05:00"


def test_a_longer_page_keeps_only_the_rows_the_plain_page_lists() -> None:
    source = PrNewswirePage("prnewswire_html")

    longer = source.parse(page(LONGER_PAGE))

    # The 16 deeper rows are older releases, which a window that started on a 25-row page
    # never saw. They mustn't look new to it.
    assert len(longer) == 25
    assert {s.key for s in longer} == {s.key for s in source.parse(page(PAGE))}


@pytest.mark.parametrize(
    ("names", "publisher", "titles"),
    [
        (["NASDAQ: JOYY"], None, ["JOYY Reports Third Quarter 2026 Financial Results"]),
        (["Palo Alto Networks"], None, ["Palo Alto Networks Reports Fiscal First Quarter"]),
        (["Lucid Group"], None, []),
        (["NYSE: BORR"], None, ["Borr Drilling Limited Announces Third Quarter"]),
        # A one-word brand matches as the headline's publisher
        (["NASDAQ: NONE"], "JOYY Inc.", ["JOYY Reports Third Quarter 2026 Financial Results"]),
    ],
)
def test_the_earnings_page_matches_companies_like_the_feed(
    names: list[str], publisher: str | None, titles: list[str]
) -> None:
    source = PrNewswirePage("prnewswire_html", companies=names, publisher=publisher)

    sightings = source.parse(page(PAGE))

    assert [s.title[: len(t)] for s, t in zip(sightings, titles, strict=True)] == titles
    # The feed's own matcher keeps the same rows, given the same headline and lead
    feed = RssFeed("prnewswire", PR_NEWSWIRE_EARNINGS, names, publisher=publisher)
    items = "".join(
        f"<item><title>{_xml(s.title)}</title><link>{s.url}</link><guid>{s.url}</guid>"
        f"<description>{_xml(s.lead)}</description></item>"
        for s in PrNewswirePage("prnewswire_html").parse(page(PAGE))
    )
    rss = f"<rss><channel><link>{PR_NEWSWIRE_EARNINGS}</link>{items}</channel></rss>"
    from_feed = feed.parse(Response(PR_NEWSWIRE_EARNINGS, 200, {}, rss.encode(), 0.1))
    assert [s.url for s in from_feed] == [s.url for s in sightings]


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;")


@pytest.mark.parametrize(
    ("body", "error"),
    [
        # Every row gone: a page that lists no releases isn't a quiet hour
        (
            re.sub(rb'<a class="newsreleaseconsolidatelink.*?</a>', b"", PAGE, flags=re.DOTALL),
            "no release rows",
        ),
        # Another listing, such as all news, isn't the Earnings category
        (
            PAGE.replace(
                b'rel="canonical" href="https://www.prnewswire.com/news-releases/financial-'
                b'services-latest-news/earnings-list/"',
                b'rel="canonical" href="https://www.prnewswire.com/news-releases/news-releases'
                b'-list/"',
            ),
            "requested listing",
        ),
        (b"<html><head><title>404 Page Not Found</title></head></html>", "requested listing"),
        (b"\x00\xff not a page", "requested listing"),
        # A row without its headline means the page changed shape
        (PAGE.replace(b"<h3", b"<h4", 1).replace(b"</h3>", b"</h4>", 1), "without a headline"),
    ],
)
def test_an_empty_or_unrecognized_page_is_an_error_not_no_release(body: bytes, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        PrNewswirePage("prnewswire_html").parse(page(body))


async def test_an_empty_page_counts_as_a_failed_poll() -> None:
    log = io.StringIO()
    journal = Journal(log)
    empty = re.sub(rb'<a class="newsreleaseconsolidatelink.*?</a>', b"", PAGE, flags=re.DOTALL)
    w = Watch("PANW", PrNewswirePage("prnewswire_html", companies=["Palo Alto Networks"]))

    await watch(
        w,
        FakeFetcher((200, empty.decode())),
        journal,
        journal.now() + timedelta(seconds=0.05),
        interval=0.01,
    )

    # No baseline was taken from it, so a later good page still has to prove what's new
    assert w.known is None
    assert w.errors == w.polls > 0
    [error, *_] = [json.loads(line) for line in log.getvalue().splitlines()]
    assert error["event"] == "error"
    assert "no release rows" in error["message"]


def test_each_poll_asks_for_a_page_size_whose_cached_copy_has_expired() -> None:
    source = PrNewswirePage("prnewswire_html")
    sizes = list(PrNewswirePage.PAGE_SIZES)

    urls = [source.url() for _ in range(3 * len(sizes))]

    asked = []
    for url in urls:
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == PR_NEWSWIRE_EARNINGS_PAGE
        query = parse_qs(parts.query)
        assert query["page"] == ["1"]
        asked.append(int(query["pagesize"][0]))
    # Never fewer rows than the plain page, and each size again only after all the others
    assert min(asked) == PrNewswirePage.PAGE_ROWS
    assert sorted(asked[: len(sizes)]) == sizes
    assert all(asked[i] == asked[i + len(sizes)] for i in range(len(asked) - len(sizes)))
    # PR Newswire's CDN keeps each copy for 60 s (max-age=60, measured Nov 19, 2026)
    assert len(sizes) * source.interval > 60


async def test_the_page_is_fetched_without_following_redirects() -> None:
    async def answer(method: str, url: str) -> tuple[int, dict[str, str], bytes]:
        return 301, {"location": PR_NEWSWIRE_EARNINGS_PAGE}, b""

    session = RedirectRecordingSession(answer)
    async with SessionFetcher(session) as fetcher:
        fetched = await PrNewswirePage("prnewswire_html").fetch(fetcher)

    # A redirect would be another page or the cached copy, so it shows as an error instead
    assert session.redirects == [False]
    assert fetched.status == 301


@pytest.mark.parametrize(
    ("listed", "answered", "published"),
    [
        ("Nov 18, 2026, 16:05 ET", "Thu, 19 Nov 2026 06:10:10 GMT", "2026-11-18T16:05:00-05:00"),
        # Not seen on Nov 19 (no row from that day yet), but a row may show the minute alone
        ("06:00 ET", "Thu, 19 Nov 2026 11:00:40 GMT", "2026-11-19T06:00:00-05:00"),
        ("23:59 ET", "Fri, 20 Nov 2026 05:00:30 GMT", "2026-11-19T23:59:00-05:00"),
        ("Coming soon", "Thu, 19 Nov 2026 11:00:40 GMT", None),
    ],
)
def test_a_rows_stamp_becomes_its_publication_time(
    listed: str, answered: str, published: str | None
) -> None:
    body = PAGE.replace(
        b"<small>Nov 18, 2026, 22:50 ET</small>", f"<small>{listed}</small>".encode()
    )

    first = PrNewswirePage("prnewswire_html").parse(page(body, {"date": answered}))[0]

    assert first.published == published
    assert first.details == {"listed": listed}


def prn_target(**fields: object) -> Target:
    return Target.model_validate(
        {
            "ticker": "PANW",
            "expected": datetime(2026, 11, 18, 16, 5, tzinfo=ET),
            "cik": 1327567,
            "prnewswire": ["Palo Alto Networks"],
            "wire_publisher": "Palo Alto Networks",
        }
        | fields
    )


def test_a_pr_newswire_target_also_watches_the_earnings_page() -> None:
    sources = {s.name: s for s in prn_target().sources()}

    page_route = sources["prnewswire_html"]
    assert isinstance(page_route, PrNewswirePage)
    assert page_route.feed_url == PR_NEWSWIRE_EARNINGS_PAGE
    # Matched like the feeds: same names, same publisher
    feed = sources["prnewswire_busted"]
    assert isinstance(feed, FreshPrNewswire)
    assert page_route.parse(page(PAGE)) == PrNewswirePage(
        "x", companies=["Palo Alto Networks"], publisher="Palo Alto Networks"
    ).parse(page(PAGE))
    # Its own poll and its own pauses, apart from both feeds'
    keys = {sources[n].share_key for n in ("prnewswire_busted", "prnewswire", "prnewswire_html")}
    assert len(keys) == 3
    assert page_route.pause_route is None
    # Another window's page route shares the one poll
    other = {s.name: s for s in prn_target(ticker="X", prnewswire=["Acme"]).sources()}
    assert other["prnewswire_html"].share_key == page_route.share_key


def test_pr_newswire_routes_keep_the_spelled_feed_at_full_speed_within_the_host_budget() -> None:
    # Broadcom's targets also read the Semiconductors feed, the most PR Newswire routes a
    # target has
    sources = prn_target(trial={"prnewswire_semiconductors": ["Broadcom Inc"]}).sources()
    on_host = {s.name: s for s in sources if "www.prnewswire.com" in s.url()}

    assert set(on_host) == {
        "prnewswire_busted",
        "prnewswire_html",
        "prnewswire",
        "prnewswire_semiconductors",
    }
    assert on_host["prnewswire_busted"].interval == 1.0
    # Every route at full speed at once still fits the host's turns, so the spelled feed
    # never waits behind the page
    assert sum(1 / s.interval for s in on_host.values()) <= 1 / host_gap("www.prnewswire.com")


class Refusing(RoutedFetcher):
    """Answers the feeds, and refuses the listing page."""

    def __init__(self, status: int) -> None:
        feed = f"<rss><channel><link>{PR_NEWSWIRE_EARNINGS}</link></channel></rss>".encode()
        super().__init__(
            {"/rss/": (200, feed), "/news-releases/": (status, b"")},
            headers={"retry-after": "3600"},
        )

    def sent(self, part: str) -> int:
        return sum(1 for _, url, _ in self.requests if part in unquote(url))


@pytest.mark.parametrize("status", [403, 429])
async def test_a_refused_earnings_page_backs_off_without_slowing_the_feeds(status: int) -> None:
    log = io.StringIO()
    journal = Journal(log)
    fetcher = Refusing(status)
    polls = Polls(fetcher, journal)
    watches = []
    for source in prn_target().sources():
        if source.name.startswith("prnewswire"):
            source.interval = 0.01
            watches.append(Watch("PANW", source))
    end = journal.now() + timedelta(seconds=0.3)

    await asyncio.wait_for(asyncio.gather(*(polls.watch(w, end) for w in watches)), 2)

    by_name = {w.source.name: w for w in watches}
    # One refusal, then the page waits out its pause (30 s after a 403, the Retry-After after
    # a 429) while both feeds keep polling
    assert fetcher.sent("/news-releases/") == by_name["prnewswire_html"].polls == 1
    assert by_name["prnewswire_busted"].polls > 5
    assert by_name["prnewswire"].polls > 5
    assert by_name["prnewswire_busted"].errors == by_name["prnewswire"].errors == 0
    errors = [e for e in map(json.loads, log.getvalue().splitlines()) if e["event"] == "error"]
    assert [e["source"] for e in errors] == ["prnewswire_html"]
    if status == 403:
        assert (by_name["prnewswire_html"].blocked, errors[0]["retry_in"]) == (1, 30.0)
    else:
        assert "backing off for 3600s" in errors[0]["message"]


async def test_a_release_first_listed_on_the_earnings_page_is_journaled_and_passed_on() -> None:
    log = io.StringIO()
    journal = Journal(log)
    source = PrNewswirePage("prnewswire_html", companies=["Palo Alto Networks"])
    before = without_row(PAGE, PANW).decode()
    w = Watch("PANW", source, (journal.now(), journal.now() + timedelta(minutes=30)))
    passed: list[tuple[Source, Sighting]] = []

    await watch(
        w,
        FakeFetcher((200, before), (200, PAGE.decode())),
        journal,
        journal.now() + timedelta(seconds=0.1),
        lambda source, item: passed.append((source, item)),
        interval=0.01,
    )

    [new] = [e for e in map(json.loads, log.getvalue().splitlines()) if e["event"] == "new"]
    assert new["source"] == "prnewswire_html"
    assert new["url"] == f"https://www.prnewswire.com/news-releases/{PANW}"
    assert new["earnings"] is True
    assert new["details"] == {"listed": "Nov 18, 2026, 16:05 ET"}
    assert "pagesize=" in new["request_url"]
    assert w.first_title == "Palo Alto Networks Reports Fiscal First Quarter 2027 Financial Results"
    [(posted_by, item)] = passed
    assert posted_by is source and item.url == new["url"]
