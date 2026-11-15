"""The bot's own investigation of a company it has no research on."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Self, cast

import pytest

from earnings_bot import date_discovery, investigate, ir_research
from earnings_bot.browser import RenderedPage
from earnings_bot.calendar import Calendar, describe
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.investigate import (
    INVESTIGATION,
    due,
    feed_links,
    ir_hosts,
    wire,
    wire_seen,
    with_wire,
)
from earnings_bot.issuers import profile
from earnings_bot.known import SourceCheck, known_companies, to_target
from earnings_bot.recorder import ET
from earnings_bot.sec_results import SixKResults
from earnings_bot.sources import Sighting
from tests.fakes import RoutedFetcher, rendered, sec_filing_detail
from tests.results_data import (
    CRM_RELEASES,
    HD_RELEASES,
    LOW_RELEASES,
    NVDA_RELEASES,
    TGT_RELEASES,
    WMT_RELEASES,
)
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS
from tests.test_date_discovery import AMAT, AMAT_NOTICE_BODY
from tests.test_pipeline import filing_routes

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 10, tzinfo=ET)
LUCID = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, TODAY)
LUCID_HOME = "https://ir.lucidmotors.com/"
LUCID_LIST = "https://ir.lucidmotors.com/press-releases"
LUCID_Q2 = "https://ir.lucidmotors.com/news-releases/news-release-details/q2-results"
# How Lucid's last results release, as filed with its 8-K, names its sites
LUCID_RELEASE = (
    "Lucid Announces Operational Reset and Second Quarter 2026 Results. A live webcast is "
    "available at ir.lucidmotors.com. Learn more at www.lucidmotors.com, or write to "
    "investor@lucidmotors.com."
)
DATELINE = "NEWARK, Calif., Aug. 4, 2026 /PRNewswire/ -- Lucid Group, Inc. (NASDAQ: LCID) today"
LUCID_Q2_TITLE = "Lucid Announces Operational Reset and Second Quarter 2026 Results"
# What makes a page read as a results report: figures, and enough of them
REPORT = (
    "<p>Revenue was $259 million. Net loss per share was $0.24.</p>"
    + "<p>Details of the quarter.</p>" * 200
)


class FakeChrome:
    """Desktop Chrome, answering from `pages`. Counts how often it's started."""

    def __init__(self, pages: dict[str, tuple[int, str]]) -> None:
        self.pages = pages
        self.started = 0
        self.calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def open(self) -> AsyncIterator[Self]:
        self.started += 1
        yield self

    async def request(
        self,
        url: str,
        warmup: str,
        give_up_after: float,
        wait_turn: Callable[[str], Awaitable[None]],
    ) -> Response:
        self.calls.append((url, warmup))
        status, body = self.pages.get(url, (404, ""))
        return Response(url, status, {}, body.encode(), 0.01)

    async def render(
        self, url: str, give_up_after: float, wait_turn: Callable[[str], Awaitable[None]]
    ) -> RenderedPage:
        self.calls.append((url, "render"))
        status, body = self.pages.get(url, (404, ""))
        return rendered(url, status, body)


def lucid_chrome() -> FakeChrome:
    return FakeChrome(
        {
            LUCID_HOME: (200, '<a href="/press-releases">Press Releases</a><a href="/">Home</a>'),
            LUCID_LIST: (
                200,
                (
                    '<a href="/news-releases/news-release-details/q3-deliveries">Lucid Announces '
                    "Q3 Production and Deliveries, Sets Date for Third Quarter 2026 Earnings</a>"
                    f'<a href="{LUCID_Q2}">Lucid Announces Operational Reset and Second Quarter '
                    "2026 Results</a>"
                ),
            ),
            LUCID_Q2: (200, f"<h1>Second Quarter 2026 Results</h1><p>{DATELINE}</p>{REPORT}"),
        }
    )


def sec_releases(releases: list[tuple[str, str]]) -> list[tuple[Sighting, str]]:
    """Results releases as `_last_releases` gives them: each 8-K, accepted when given, and its
    text."""
    return [
        (Sighting(accepted, "8-K 2.02,9.01", "https://www.sec.gov/", accepted, True), text)
        for accepted, text in releases
    ]


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Sets the text of the company's last results release, as SEC has it."""

    def set_text(text: str) -> None:
        async def last_releases(fetcher: object, cik: int) -> list[tuple[Sighting, str]]:
            return sec_releases([("2026-08-04T20:07:05.000Z", text)] if text else [])

        monkeypatch.setattr(investigate, "_last_releases", last_releases)

    return set_text


def test_ir_hosts_are_the_investor_sites_a_release_names() -> None:
    assert ir_hosts(LUCID_RELEASE) == ["ir.lucidmotors.com"]
    text = "See investor.apple.com, investors.palantir.com, sec.gov and Investors.Example.com."
    assert ir_hosts(text) == [
        "investor.apple.com",
        "investors.palantir.com",
        "investors.example.com",
    ]
    assert ir_hosts("U.S. revenue grew 5.2%. Visit www.example.com.") == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        (DATELINE, "PR Newswire"),
        ("AUSTIN, Texas--(BUSINESS WIRE)--Example Corp today", "Business Wire"),
        ("SANTA CLARA, Calif., Nov. 3, 2026 (GLOBE NEWSWIRE) -- AMD today", "GlobeNewswire"),
        ("Example Corp today reported results.", None),
        # Only a dateline counts, not a mention far into the release
        ("Results. " * 300 + "(BUSINESS WIRE)", None),
    ],
    ids=["pr-newswire", "business-wire", "globenewswire", "none", "late-mention"],
)
def test_the_wire_comes_from_the_dateline(text: str, name: str | None) -> None:
    assert wire(text) == name


def test_feed_links_put_the_advertised_feed_first_and_stay_on_the_site() -> None:
    page = (
        '<link rel="alternate" type="application/rss+xml" href="/feeds/press.xml">'
        '<link rel="alternate" type="application/atom+xml" href="https://elsewhere.example/a">'
        '<link rel="stylesheet" href="/style.css">'
    )

    assert feed_links("https://ir.example.com/", page) == [
        "https://ir.example.com/feeds/press.xml",
        "https://ir.example.com/rss/news-releases.xml",
        "https://ir.example.com/news-releases/rss",
        "https://ir.example.com/press-releases/rss",
        "https://ir.example.com/news-events/press-releases/rss",
    ]


def q4_list(*releases: dict[str, str]) -> tuple[int, bytes]:
    return 200, json.dumps({"GetPressReleaseListResult": list(releases)}).encode()


Q4_RESULTS = {
    "PressReleaseId": "1",
    "Headline": "Example Reports Third Quarter 2026 Results",
    "LinkToDetailPage": "/news/q3",
    "Body": "<p>SAN JOSE, Calif.--(BUSINESS WIRE)--Example Corp (NYSE: EXM) today reported</p>",
}


async def test_a_q4_site_is_polled_directly(released: Callable[[str], None]) -> None:
    # The first site named doesn't exist, so the next is tried
    released("Webcast at ir.gone.example; materials at investors.example.com.")
    fetcher = RoutedFetcher(
        {
            "investors.example.com/feed/PressRelease.svc": q4_list(Q4_RESULTS),
            "investors.example.com/": (200, b"<html>Investors</html>"),
        }
    )
    chrome = FakeChrome({})

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert (found.platform, found.q4_host, found.reachable) == ("q4", "investors.example.com", True)
    assert (found.rss, found.ir_listing, found.wire) == ([], None, "Business Wire")
    assert found.investigated == TODAY
    assert found.evidence == (
        "Investigated Nov 09: Q4 press feed on investors.example.com. Wire: Business Wire."
    )
    # The IR site is opened in Chrome even with a feed to poll
    assert chrome.started == 1
    assert [(c.source, c.watch, c.why) for c in found.checks] == [
        (
            "SEC 8-K filings",
            True,
            f"Its last results 8-Ks were accepted at {', '.join(LUCID.eightk)} ET",
        ),
        ("ir.gone.example homepage over HTTP", False, "Returned HTTP 404"),
        ("Q4 press release feed on ir.gone.example", False, "Not a Q4 site (HTTP 404)"),
        ("Q4 press release feed on investors.example.com", True, "Lists its releases and files"),
        (
            "investors.example.com press release page",
            False,
            "Chrome got HTTP 404 from investors.example.com",
        ),
        (
            "PR Newswire earnings feed",
            False,
            "Its releases go out on Business Wire",
        ),
        (
            "Business Wire earnings feed",
            True,
            "Its releases go out on Business Wire.",
        ),
        (
            "GlobeNewswire earnings feed",
            False,
            "Its releases go out on Business Wire",
        ),
    ]
    target = to_target("LCID", NOW, 2026, 4, found)
    assert [s.name for s in target.sources()] == [
        "q4_press",
        "q4_files",
        "edgar",
        "edgar_latest",
        "businesswire_busted",
    ]


async def test_blocked_q4_homepage_does_not_hide_the_issuer_notice(
    released: Callable[[str], None],
) -> None:
    host = "investors.on-running.com"
    home = f"https://{host}/"
    canonical = home + "home/default.aspx"
    listing = home + "news/default.aspx"
    notice = {
        "PressReleaseId": "2",
        "Headline": "On to Release Third Quarter 2026 Results on Tuesday, November 10, 2026",
        "LinkToDetailPage": "/news/q3-notice",
        "Body": (
            "<p>On will release its third quarter 2026 financial results on Tuesday, "
            "November 10, 2026 before U.S. financial markets open.</p>"
            "<p>On will host a conference call at 8 a.m. U.S. Eastern Time "
            "on November 10, 2026.</p>"
        ),
    }
    fetcher = RoutedFetcher(
        {f"{host}/feed/PressRelease.svc": q4_list(notice, Q4_RESULTS), home: (403, b"Blocked")}
    )
    chrome = FakeChrome(
        {
            home: (0, ""),
            canonical: (200, "<h1>Investor Relations</h1>"),
            listing: (200, '<a href="/news/q2">On Reports Results for Second Quarter 2026</a>'),
            home + "news/q2": (200, REPORT),
        }
    )
    released(f"Issuer results. See {home}.")
    company = LUCID.model_copy(update={"release_time": "06:10"})
    found = await investigate.investigate(fetcher, company, TODAY, chrome.open)

    assert (found.q4_host, found.reachable, found.ir_http_status) == (host, True, 403)
    assert chrome.calls[:3] == [(home, canonical), (canonical, canonical), (listing, canonical)]
    result = await date_discovery.find_date(fetcher, "ONON", NOW, found)
    assert result and result[0].period == "FY2026Q3"
    assert result[0].expected == datetime(2026, 11, 10, 6, 10, tzinfo=ET)
    # Before the open runs to 9:35, whatever past filing times say
    assert result[0].window == (
        datetime(2026, 11, 10, 5, 10, tzinfo=ET),
        datetime(2026, 11, 10, 9, 35, tzinfo=ET),
    )
    assert result[1] == f"Issuer date confirmed, before market open <{home}news/q3-notice>"
    no_history = date_discovery.parse_notice(
        "ONON",
        found.model_copy(update={"release_time": None}),
        notice["Headline"],
        notice["Body"],
        NOW,
    )
    assert no_history and no_history[0].expected.hour == 7


async def test_a_site_with_a_feed_is_polled_through_it(released: Callable[[str], None]) -> None:
    released("Webcast at ir.example.com.")
    feed = """<rss version="2.0"><channel>
    <item><title>Example Reports Third Quarter 2026 Results</title>
    <link>https://ir.example.com/news/q3</link><guid>q3</guid></item>
    </channel></rss>"""
    fetcher = RoutedFetcher(
        {
            "ir.example.com/feeds/press.xml": (200, feed.encode()),
            "ir.example.com/news/q3": (200, f"<p>{DATELINE}</p>".encode()),
            "ir.example.com/feed/PressRelease.svc": (404, b""),
            "ir.example.com/": (
                200,
                b'<link rel="alternate" type="application/rss+xml" href="/feeds/press.xml">',
            ),
        }
    )

    found = await investigate.investigate(fetcher, LUCID, TODAY, FakeChrome({}).open)

    assert (found.platform, found.q4_host, found.reachable) == ("own-site", None, True)
    assert (found.rss, found.wire) == (["https://ir.example.com/feeds/press.xml"], "PR Newswire")
    assert found.evidence == (
        "Investigated Nov 09: Press release feed <https://ir.example.com/feeds/press.xml>. "
        "Wire: PR Newswire."
    )
    target = to_target("LCID", NOW, 2026, 4, found)
    assert [s.name for s in target.sources()] == [
        "edgar",
        "edgar_latest",
        "prnewswire_busted",
        "prnewswire",
        "rss:ir.example.com",
    ]


@pytest.mark.parametrize("home_status", [200, 403], ids=["readable-home", "blocked-home"])
@pytest.mark.parametrize("valid_feed", [True, False], ids=["rss", "html-error"])
async def test_feeds_on_the_actual_ir_listing_are_checked(
    released: Callable[[str], None], home_status: int, valid_feed: bool
) -> None:
    # The feed is advertised on the newsroom, outside the guessed root-level paths.
    home = "https://ir.example.com/"
    listing = home + "news/press-releases"
    feed = listing + "/rss"
    article = listing + "/detail/1/quarterly-results"
    title = "Example Reports Third Quarter 2026 Results"
    page = (
        f'<link rel="alternate" type="application/rss+xml" href="{feed}">'
        f'<a href="{article}">{title}</a>'
    )
    rss = (
        f"<rss><channel><title>Issuer news</title><item><title>{title}</title>"
        f"<link>{article}</link><guid>q3</guid></item></channel></rss>"
    )
    released("Webcast at ir.example.com.")
    fetcher = RoutedFetcher(
        {
            feed: (200, (rss if valid_feed else "<html>Not a feed</html>").encode()),
            article: (200, f"<h1>{title}</h1><p>{DATELINE}</p>{REPORT}".encode()),
            listing: (200, page.encode()),
            home: (home_status, b"<html>Investor home</html>"),
        }
    )
    chrome = FakeChrome(
        {
            home: (200, f'<a href="{listing}">Press Releases</a>'),
            listing: (200, page),
            article: (200, f"<h1>{title}</h1>{REPORT}"),
        }
    )

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert found.ir_listing == listing
    assert found.rss == ([feed] if valid_feed else [])
    assert any(url == feed for _, url, _ in fetcher.requests)
    if valid_feed:
        assert found.wire == "PR Newswire"
        assert any(c.watch and feed in c.source for c in found.checks)
        assert any(
            s.name.startswith("rss:") for s in to_target("LCID", NOW, 2026, 4, found).sources()
        )


async def test_a_site_that_blocks_the_bot_is_read_through_chrome(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    chrome = lucid_chrome()

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert (found.platform, found.reachable, found.ir_listing) == ("own-site", False, LUCID_LIST)
    assert (found.q4_host, found.rss, found.wire) == (None, [], "PR Newswire")
    assert found.evidence == (
        "Investigated Nov 09: ir.lucidmotors.com blocks the bot, so Chrome reads "
        f"<{LUCID_LIST}>. Wire: PR Newswire."
    )
    # One session follows the site; a second checks the vendor's optional RSS endpoint.
    assert chrome.started == 2
    assert chrome.calls == [
        (LUCID_HOME, LUCID_HOME),
        (LUCID_LIST, LUCID_HOME),
        (LUCID_Q2, LUCID_HOME),
        (LUCID_HOME + "rss/news-releases.xml", LUCID_LIST),
    ]
    # The wire is known now, so only PR Newswire is watched
    target = to_target("LCID", NOW, 2026, 3, found)
    assert [s.name for s in target.sources()] == [
        "edgar",
        "edgar_latest",
        "prnewswire_busted",
        "prnewswire",
    ]


async def test_without_chrome_a_blocked_site_is_reported_and_both_wires_kept(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})

    found = await investigate.investigate(fetcher, LUCID, TODAY, None)

    assert (found.platform, found.ir_listing, found.wire) == ("own-site", None, "unknown")
    assert found.evidence == (
        "Investigated Nov 09: ir.lucidmotors.com blocks the bot, and Chrome couldn't read it "
        "either. Wire not found, so all three are watched."
    )
    target = to_target("LCID", NOW, 2026, 3, found)
    assert target.newswire == target.prnewswire == LUCID.names


async def test_a_release_naming_no_ir_site_still_gives_its_wire(
    released: Callable[[str], None],
) -> None:
    released("AUSTIN, Texas--(BUSINESS WIRE)--Example Corp today reported. See www.example.com.")
    fetcher = RoutedFetcher({})

    found = await investigate.investigate(fetcher, LUCID, TODAY, FakeChrome({}).open)

    assert fetcher.requests == []
    assert (found.platform, found.wire, found.investigated) == ("unknown", "Business Wire", TODAY)
    assert found.evidence == (
        "Investigated Nov 09: No issuer press release site could be verified. Wire: Business Wire."
    )


async def test_an_issuer_named_rss_index_reaches_a_separate_newsroom(
    released: Callable[[str], None],
) -> None:
    home = "https://investor.cisco.com/"
    index = "https://newsroom.cisco.com/rss-feeds"
    canonical = "https://newsroom.cisco.com/press-room/rss-feeds.html"
    all_news = "https://newsroom.cisco.com/services/rssfeed.json"
    press = all_news + "?feed=press-releases"
    notice = "https://newsroom.cisco.com/notice"
    title = "Cisco Schedules Conference Call for Q1 Fiscal Year 2027 Financial Results"
    feed = f"<rss><channel><item><title>{title}</title><link>{notice}</link></item></channel></rss>"
    released(f"Full financial results are available at {home}. RSS Feed for Cisco: {index}.")
    fetcher = RoutedFetcher(
        {
            home: (403, b""),
            index: (302, b""),
            canonical: (
                200,
                f'<a href="{all_news}">All news</a><a href="{press}">Press</a>'.encode(),
            ),
            press: (200, feed.encode()),
            notice: (
                200,
                b"Results will be released after the close of the market on Nov 12, 2026.",
            ),
        },
        headers={"location": canonical},
    )
    found = await investigate.investigate(fetcher, LUCID, TODAY, None, ticker="CSCO")

    assert found.rss == [press]
    assert any(check.watch and press in check.source for check in found.checks)
    result = await date_discovery.find_date(fetcher, "CSCO", NOW, found)
    assert result and result[0].period == "FY2027Q1"
    assert result[0].expected.date() == date(2026, 11, 12)
    assert result[1] == f"Issuer date confirmed, after market close <{notice}>"
    assert [url for _, url, _ in fetcher.requests if url == all_news] == []


async def test_a_globenewswire_company_is_matched_there(
    released: Callable[[str], None],
) -> None:
    released("SANTA CLARA, Calif., Nov. 3, 2026 (GLOBE NEWSWIRE) -- Example today reported")

    found = await investigate.investigate(RoutedFetcher({}), LUCID, TODAY, None)

    target = to_target("LCID", NOW, 2026, 3, found)
    assert (target.newswire, target.prnewswire) == ([], [])
    assert target.trial.globenewswire == LUCID.names
    assert "globenewswire" in [s.name for s in target.sources()]


async def test_results_filed_under_other_events_are_found_by_their_exhibits() -> None:
    """Brookfield Asset Management files results, dividends and deals all under Item 8.01."""
    cik = 1937926
    filings = [
        ("0001171843-26-007300", "8-K", "8.01,9.01", "2026-11-10T21:00:00.000Z", "f8k.htm"),
        ("0001171843-26-007210", "8-K", "8.01,9.01", "2026-11-06T11:00:00.000Z", "f8k.htm"),
    ]
    columns = ("accessionNumber", "form", "items", "acceptanceDateTime", "primaryDocument")
    recent = {column: [row[i] for row in filings] for i, column in enumerate(columns)}
    routes: dict[str, tuple[int, bytes]] = {
        f"submissions/CIK{cik:010d}.json": (
            200,
            json.dumps({"filings": {"recent": recent}}).encode(),
        )
    }
    bodies = {
        "0001171843-26-007300": "<h1>Brookfield Asset Management Declares Quarterly Dividend</h1>"
        + "<p>The board declared a quarterly dividend of $0.4375 per share.</p>" * 40,
        "0001171843-26-007210": "<h1>Brookfield Asset Management Reports Q3 2026 Results</h1>"
        "<p>Brookfield Asset Management today announced financial results for the quarter "
        "ended September 30, 2026.</p>"
        + "<p>Revenues of $1.2 billion and net income of $600 million. </p>"
        * 35,
    }
    for key, body in bodies.items():
        base = f"/Archives/edgar/data/{cik}/{key.replace('-', '')}"
        routes[f"{base}/{key}-index.htm"] = (200, sec_filing_detail(f"{base}/f8k.htm", "ex.htm"))
        routes[f"{base}/ex.htm"] = (200, body.encode())

    releases = await investigate._last_releases(RoutedFetcher(routes), cik)

    assert [filing.key for filing, _ in releases] == ["0001171843-26-007210"]
    assert "Revenues of $1.2 billion" in releases[0][1]


async def test_the_last_release_is_read_from_the_results_8k() -> None:
    # Newest first: a busy issuer can have dozens of ownership filings after its results.
    filings = [
        *(
            (f"0001543151-26-{i:06d}", "4", "", "2026-11-09T21:00:00.000Z", "form4.xml")
            for i in range(99, 54, -1)
        ),
        ("0001543151-26-000042", "8-K", "5.07", "2026-11-06T21:00:00.000Z", "uber-vote.htm"),
        ("0001543151-26-000041", "10-Q", "", "2026-11-05T21:00:00.000Z", "uber-10q.htm"),
        (
            "0001543151-26-000040",
            "8-K",
            "2.02,9.01",
            "2026-11-04T11:55:39.000Z",
            "uber-20261104.htm",
        ),
    ]
    columns = ("accessionNumber", "form", "items", "acceptanceDateTime", "primaryDocument")
    recent = {column: [row[i] for row in filings] for i, column in enumerate(columns)}
    submissions = json.dumps({"filings": {"recent": recent}}).encode()
    fetcher = RoutedFetcher(
        {"submissions/CIK0001543151.json": (200, submissions), **filing_routes()}
    )

    [(filing, text)] = await investigate._last_releases(fetcher, 1543151)

    assert "Gross bookings grew 21%" in text
    assert filing.published == "2026-11-04T11:55:39.000Z"
    # Only the results 8-K is read
    assert not any("000154315126000041" in url for _, url, _ in fetcher.requests)
    assert not any("000154315126000042" in url for _, url, _ in fetcher.requests)
    assert not any("form4.xml" in url for _, url, _ in fetcher.requests)


def test_only_companies_outside_the_catalog_are_investigated_and_then_monthly() -> None:
    assert not due("NVDA", known_companies()["NVDA"], TODAY)
    assert due("LCID", LUCID, TODAY)
    checked = [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")]
    looked = LUCID.model_copy(
        update={
            "investigated": date(2026, 10, 11),
            "checks": checked,
            "investigation": INVESTIGATION,
        }
    )
    assert not due("LCID", looked, TODAY)
    assert due("LCID", looked, date(2026, 11, 10))
    # Looked at by an older version of the investigation, which finds less
    assert due("LCID", looked.model_copy(update={"investigation": INVESTIGATION - 1}), TODAY)


NOTICE = {
    "PressReleaseId": "2",
    "Headline": "Lucid to Announce Third Quarter 2026 Financial Results",
    "LinkToDetailPage": "/news/notice",
    "Body": "<p>Lucid will report its third quarter 2026 financial results after the market "
    "close on November 12, 2026.</p>",
}


async def test_discovery_investigates_once_then_finds_the_date_on_the_new_feed(
    tmp_path: Path, released: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unverified(*args: object) -> tuple[None, str]:
        return None, "Not checked in tests"

    monkeypatch.setattr(date_discovery, "verified_browser_source", unverified)
    released("Webcast at investors.example.com.")
    fetcher = RoutedFetcher(
        {
            "investors.example.com/feed/PressRelease.svc": q4_list(NOTICE, Q4_RESULTS),
            "investors.example.com/": (200, b"<html>Investors</html>"),
        }
    )
    statuses: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", LUCID)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, statuses.append)
        [row] = calendar.entries()

        await dates._check(row)

        [row] = calendar.entries()
        assert row.company.q4_host == "investors.example.com"
        assert row.target is not None
        assert row.target.expected == datetime(2026, 11, 12, 16, 5, tzinfo=ET)
        assert row.evidence == (
            "Issuer date confirmed, after market close <https://investors.example.com/news/notice>"
        )
        assert statuses == [f"📅 {describe(row.target, row.evidence)}"]
        assert "q4_press" in [s.name for s in row.target.sources()]

        # The next hourly check uses what was found without looking again
        homepage = len([url for _, url, _ in fetcher.requests if url.endswith(".com/")])
        await dates._check(row)
        assert len([url for _, url, _ in fetcher.requests if url.endswith(".com/")]) == homepage


async def test_findings_from_a_check_cut_short_by_a_window_arent_saved(
    tmp_path: Path, released: Callable[[str], None]
) -> None:
    released(LUCID_RELEASE)
    window_open = False
    chrome = lucid_chrome()
    real_request = chrome.request

    async def window_opens(*args: object, **kwargs: object) -> Response:
        nonlocal window_open
        window_open = True
        return await real_request(*args, **kwargs)  # type: ignore[arg-type]

    chrome.request = window_opens  # type: ignore[method-assign]
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    statuses: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", LUCID)
        dates = DateDiscovery(
            calendar, fetcher, lambda: NOW, lambda: window_open, statuses.append, None, chrome.open
        )
        [row] = calendar.entries()

        await dates._check(row)

        [after] = calendar.entries()
        assert after.company == LUCID
    assert statuses == []


async def test_every_source_checked_is_reported_with_why_as_it_goes(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    steps: list[str] = []
    counts: list[int] = []
    report = investigate.Report()

    def changed() -> None:
        steps.append(report.step)
        counts.append(len(report.checks))

    report.changed = changed

    found = await investigate.investigate(fetcher, LUCID, TODAY, lucid_chrome().open, report=report)

    assert found.checks == report.checks
    assert found.ir_http_status == 403
    assert [(c.source, c.watch, c.why) for c in found.checks] == [
        (
            "SEC 8-K filings",
            True,
            f"Its last results 8-Ks were accepted at {', '.join(LUCID.eightk)} ET",
        ),
        ("ir.lucidmotors.com homepage over HTTP", False, "Returned HTTP 403"),
        (
            "Q4 press release feed on ir.lucidmotors.com",
            False,
            "Not readable as a Q4 site: its firewall answered HTTP 403",
        ),
        (
            f"Press release page <{LUCID_LIST}>, read in Chrome",
            True,
            f"Chrome read its latest results, “{LUCID_Q2_TITLE}”",
        ),
        ("PR Newswire earnings feed", True, "Its releases go out on PR Newswire."),
        (
            "Business Wire earnings feed",
            False,
            "Its releases go out on PR Newswire",
        ),
        (
            "GlobeNewswire earnings feed",
            False,
            "Its releases go out on PR Newswire",
        ),
    ]
    # The scorecard hears of each finding as it's made
    assert sorted(set(counts)) == list(range(len(found.checks) + 1))
    # Each step is announced as it starts
    assert list(dict.fromkeys(steps)) == [
        "Reading its SEC filings",
        "Reading its last results 8-K on SEC",
        "Checking ir.lucidmotors.com",
        "Looking for a Q4 press release feed on ir.lucidmotors.com",
        "Opening ir.lucidmotors.com in Chrome",
        "Checking direct HTTP access to the press release listing",
        "Choosing its sources",
    ]


async def test_chrome_tries_the_usual_list_when_the_home_page_doesnt_link_it(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    chrome = lucid_chrome()
    chrome.pages[LUCID_HOME] = (200, "<a href='/'>Home</a>")

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert found.ir_listing == LUCID_LIST
    assert [url for url, _ in chrome.calls] == [
        LUCID_HOME,
        LUCID_LIST,
        LUCID_Q2,
        LUCID_HOME + "rss/news-releases.xml",
    ]


async def test_chrome_tries_another_listing_after_a_timeout(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({LUCID_HOME: (403, b"Access Denied")})
    slow = LUCID_HOME + "company/news-releases"

    class SlowChrome(FakeChrome):
        async def request(
            self,
            url: str,
            warmup: str,
            give_up_after: float,
            wait_turn: Callable[[str], Awaitable[None]],
        ) -> Response:
            if url == slow:
                raise TimeoutError
            return await super().request(url, warmup, give_up_after, wait_turn)

    chrome = SlowChrome(lucid_chrome().pages)
    chrome.pages[LUCID_HOME] = (200, f'<a href="{slow}">Press Releases</a>')

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert found.ir_release == LUCID_Q2


async def test_a_list_without_results_is_kept_for_notices_but_reported(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    chrome = lucid_chrome()
    chrome.pages[LUCID_LIST] = (200, '<a href="/news/notice">Lucid to Host Investor Day</a>')

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    # Date notices are read from the list, but nothing on it is watched for results yet
    assert found.ir_listing == LUCID_LIST
    assert (
        "ir.lucidmotors.com press release page",
        False,
        f"<{LUCID_LIST}> shows no results headline",
    ) in [(c.source, c.watch, c.why) for c in found.checks]


async def test_a_site_without_a_q4_feed_or_rss_says_what_was_tried(
    released: Callable[[str], None],
) -> None:
    released("Webcast at ir.example.com.")
    fetcher = RoutedFetcher(
        {
            "ir.example.com/feed/PressRelease.svc": (404, b""),
            "/rss": (404, b""),
            "ir.example.com/": (200, b"<html>"),
        }
    )

    found = await investigate.investigate(fetcher, LUCID, TODAY, None)

    checks = {c.source: c.why for c in found.checks if not c.watch}
    assert checks["Q4 press release feed on ir.example.com"] == "Not a Q4 site (HTTP 404)"
    assert checks["Press release feed on ir.example.com"] == (
        "None found: /rss/news-releases.xml HTTP 404, /news-releases/rss HTTP 404, "
        "/press-releases/rss HTTP 404, /news-events/press-releases/rss HTTP 404"
    )
    assert checks["ir.example.com press release page"] == (
        "Chrome isn't available on this computer"
    )
    # The wire isn't named anywhere, so all three are watched
    assert [c.source for c in found.checks if c.watch] == [
        "SEC 8-K filings",
        "PR Newswire earnings feed",
        "Business Wire earnings feed",
        "GlobeNewswire earnings feed",
    ]


async def test_a_release_naming_no_ir_site_says_so(released: Callable[[str], None]) -> None:
    released("Results. See www.example.com.")
    found = await investigate.investigate(RoutedFetcher({}), LUCID, TODAY, None)
    assert (
        "IR site",
        False,
        "No IR site or issuer website identified in its last results release",
    ) in [(c.source, c.watch, c.why) for c in found.checks]

    released("")
    found = await investigate.investigate(RoutedFetcher({}), LUCID, TODAY, None)
    assert ("IR site", False, "Couldn't read its last results 8-K") in [
        (c.source, c.watch, c.why) for c in found.checks
    ]


async def test_discovery_shows_the_investigation_while_it_runs(
    tmp_path: Path, released: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unverified(*args: object) -> tuple[None, str]:
        return None, "Not checked in tests"

    monkeypatch.setattr(date_discovery, "verified_browser_source", unverified)
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    chrome = lucid_chrome()
    seen: list[tuple[str | None, int]] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", LUCID)
        dates = DateDiscovery(
            calendar, fetcher, lambda: NOW, lambda: False, print, None, chrome.open
        )

        def progressed() -> None:
            report = dates.reports.get(LCID_CIK)
            seen.append((report.step if report else None, len(report.checks) if report else 0))

        dates.on_progress = progressed
        [row] = calendar.entries()
        await dates.run_once(row)

        assert dates.reports == {}
        [row] = calendar.entries()
        assert row.company.checks
    # The scorecard is told at each step and each finding, while the report is there to read
    assert ("Opening ir.lucidmotors.com in Chrome", 3) in seen
    assert max(count for _, count in seen) == len(row.company.checks)


async def test_without_chrome_an_open_site_keeps_the_list_a_plain_request_finds(
    released: Callable[[str], None],
) -> None:
    released("Webcast at ir.example.com.")
    fetcher = RoutedFetcher(
        {
            "ir.example.com/feed/PressRelease.svc": (404, b""),
            "/rss": (404, b""),
            "ir.example.com/": (200, b'<a href="/press-releases">Press Releases</a>'),
        }
    )

    found = await investigate.investigate(fetcher, LUCID, TODAY, None)

    # Kept for reading date notices, though nothing on it is watched for results
    assert found.ir_listing == "https://ir.example.com/press-releases"


async def test_a_menu_link_that_looks_like_results_isnt_taken_for_them(
    released: Callable[[str], None],
) -> None:
    released(LUCID_RELEASE)
    fetcher = RoutedFetcher({"ir.lucidmotors.com/": (403, b"Access Denied")})
    chrome = lucid_chrome()
    menu = "https://ir.lucidmotors.com/financials/quarterly-results"
    chrome.pages[LUCID_LIST] = (
        200,
        f'<a href="{menu}">Quarterly Results</a><a href="{LUCID_Q2}">{LUCID_Q2_TITLE}</a>',
    )
    chrome.pages[menu] = (200, "<h1>Quarterly Results</h1><p>Choose a year.</p>")

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert (
        f"Press release page <{LUCID_LIST}>, read in Chrome",
        True,
        f"Chrome read its latest results, “{LUCID_Q2_TITLE}”",
    ) in [(c.source, c.watch, c.why) for c in found.checks]

    # With only the menu link, it says what it opened instead
    chrome.pages[LUCID_LIST] = (200, f'<a href="{menu}">Quarterly Results</a>')
    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)
    assert (
        "ir.lucidmotors.com press release page",
        False,
        "Chrome opened “Quarterly Results” isn't a results report",
    ) in [(c.source, c.watch, c.why) for c in found.checks]


AMAT_HOME = "https://ir.appliedmaterials.com/"
AMAT_LIST = "https://ir.appliedmaterials.com/news-releases"
AMAT_NOTICE = AMAT_LIST + "/news-release-details/applied-materials-report-fourth-quarter"
AMAT_Q3 = AMAT_LIST + "/news-release-details/applied-materials-announces-third-quarter-2026"
AMAT_NOTICE_TITLE = (
    "Applied Materials to Report Fourth Quarter and Fiscal 2026 Results on Nov. 12, 2026"
)


def amat_chrome(
    results_page: bool = True, title: str = "Applied Materials Announces Third Quarter 2026 Results"
) -> FakeChrome:
    """Applied Materials' IR site: the newest page of its list has only the date notice, and
    its last results are on the next page."""
    return FakeChrome(
        {
            AMAT_HOME: (200, '<a href="/news-releases">News</a>'),
            AMAT_LIST: (
                200,
                f'<a href="{AMAT_NOTICE}">{AMAT_NOTICE_TITLE}</a>'
                '<a href="/news-releases/news-release-details/kioxia">Applied Materials and '
                "KIOXIA Partner at the EPIC Center</a>"
                + ('<a href="?page=1">Next page</a>' if results_page else ""),
            ),
            AMAT_LIST + "?page=1": (
                200,
                (
                    f'<a href="{AMAT_Q3}">{title}</a>'
                    '<a href="?page=0">Previous page</a><a href="?page=2">Next page</a>'
                ),
            ),
            AMAT_Q3: (
                200,
                (
                    f"<h1>{title}</h1>"
                    "<p>Aug 13, 2026 at 4:01 PM EDT</p><p>SANTA CLARA, Calif., Aug. 13, 2026 "
                    f"(GLOBE NEWSWIRE) -- Applied Materials, Inc. today reported</p>{REPORT}"
                ),
            ),
            AMAT_NOTICE: (
                200,
                (
                    f"<h1>{AMAT_NOTICE_TITLE}</h1><p>SANTA CLARA, Calif., Oct. 22, 2026 "
                    "(GLOBE NEWSWIRE) -- Applied Materials, Inc. today announced</p>"
                ),
            ),
        }
    )


async def test_chrome_looks_past_a_date_notice_to_the_last_results_and_their_time(
    released: Callable[[str], None],
) -> None:
    # Its 8-K exhibit names the IR site but carries no dateline
    released("Applied Materials Announces Third Quarter 2026 Results. ir.appliedmaterials.com")
    fetcher = RoutedFetcher({"ir.appliedmaterials.com/": (403, b"Access Denied")})
    chrome = amat_chrome()

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert [url for url, _ in chrome.calls] == [
        AMAT_HOME,
        AMAT_LIST,
        AMAT_LIST + "?page=1",
        AMAT_Q3,
        "https://ir.appliedmaterials.com/rss/news-releases.xml",
    ]
    assert (found.ir_listing, found.ir_release) == (AMAT_LIST, AMAT_Q3)
    assert found.ir_posted == datetime(2026, 8, 13, 16, 1, tzinfo=ET)
    assert found.wire == "GlobeNewswire"
    assert (
        f"Press release page <{AMAT_LIST}>, read in Chrome",
        True,
        (
            "Chrome read its latest results, “Applied Materials Announces Third Quarter 2026 "
            "Results”, posted Aug 13 at 4:01 PM ET"
        ),
    ) in [(c.source, c.watch, c.why) for c in found.checks]


async def test_preliminary_results_dont_give_the_release_time(
    released: Callable[[str], None],
) -> None:
    # Construction Partners' preliminary results came at 8:30 AM, two weeks ahead of full
    # results it had put out at 7:00 or 7:30 AM for six quarters
    title = "Applied Materials Announces Preliminary Third Quarter 2026 Results"
    released(f"{title}. ir.appliedmaterials.com")
    fetcher = RoutedFetcher({"ir.appliedmaterials.com/": (403, b"Access Denied")})

    found = await investigate.investigate(fetcher, LUCID, TODAY, amat_chrome(title=title).open)

    assert (found.ir_listing, found.ir_release) == (AMAT_LIST, AMAT_Q3)
    assert found.ir_posted is None


async def test_a_date_notice_alone_keeps_both_live_wires(released: Callable[[str], None]) -> None:
    released("Results. ir.appliedmaterials.com")
    fetcher = RoutedFetcher({"ir.appliedmaterials.com/": (403, b"Access Denied")})

    found = await investigate.investigate(fetcher, LUCID, TODAY, amat_chrome(False).open)

    assert found.wire == "unknown"
    target = to_target("LCID", NOW, 2026, 4, found)
    assert target.prnewswire == target.newswire == LUCID.names
    assert found.ir_posted is None
    assert (
        "ir.appliedmaterials.com press release page",
        False,
        f"<{AMAT_LIST}> shows a date notice, “{AMAT_NOTICE_TITLE}”, but no results release to read",
    ) in [(c.source, c.watch, c.why) for c in found.checks]


async def test_only_verified_chrome_results_set_the_wire(
    released: Callable[[str], None],
) -> None:
    released("Results. ir.appliedmaterials.com")
    fetcher = RoutedFetcher({AMAT_HOME: (403, b"Access Denied")})
    chrome = amat_chrome()
    unavailable = AMAT_LIST + "/unavailable"
    navigation = AMAT_LIST + "/quarterly"
    chrome.pages[AMAT_LIST] = (
        200,
        (
            f'<a href="{unavailable}">Recent Earnings Results</a>'
            f'<a href="{navigation}">Quarterly Results</a>'
            f'<a href="{AMAT_Q3}">Applied Materials Announces Third Quarter 2026 Results</a>'
        ),
    )
    chrome.pages[unavailable] = (404, "<p>(GLOBE NEWSWIRE) --</p>" + REPORT)
    chrome.pages[navigation] = (200, "<p>Quarterly Results</p>")
    chrome.pages[AMAT_Q3] = (200, f"<p>{DATELINE}</p>{REPORT}")

    found = await investigate.investigate(fetcher, LUCID, TODAY, chrome.open)

    assert found.ir_release == AMAT_Q3
    assert found.wire == "PR Newswire"


async def test_amat_notice_in_chrome_saves_the_window_and_prepares_live_ir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = RoutedFetcher({AMAT_HOME: (403, b"Access Denied")})
    chrome = amat_chrome()
    chrome.pages[AMAT_NOTICE] = (200, AMAT_NOTICE_BODY)
    monkeypatch.setattr(fetcher, "research_browser", chrome.open)
    monkeypatch.setattr(date_discovery, "chrome_available", lambda: True)
    monkeypatch.setattr(ir_research, "chrome_available", lambda: True)
    company = AMAT.model_copy(
        update={
            "ir_listing": AMAT_LIST,
            "sec_form": "8-K",
            "investigated": TODAY,
            "investigation": INVESTIGATION,
        }
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("AMAT", company)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, lambda _: None)
        await dates._check(row)
        [planned] = calendar.entries()

    assert planned.target is not None
    assert planned.target.expected == datetime(2026, 11, 12, 16, tzinfo=ET)
    assert planned.target.period == "FY2026Q4"
    assert planned.evidence == f"Issuer date confirmed, release time estimated <{AMAT_NOTICE}>"
    assert planned.target.ir and planned.target.ir.listing == AMAT_LIST
    assert planned.target.ir.verify_text


async def test_corporate_site_with_a_hosted_release_list_schedules_transdigm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, released: Callable[[str], None]
) -> None:
    home = "https://www.transdigm.com/"
    investors = home + "investor-relations/"
    wrong = home + "news-releases/"
    wrapper = investors + "news-releases/"
    hosted = "https://transdigmgroupinc.gcs-web.com/news-releases/"
    article = hosted + "news-release-details/third-quarter-results"
    notice = hosted + "news-release-details/fourth-quarter-earnings-date"
    notice_title = (
        "TransDigm Fourth Quarter Earnings Report and Conference Call Set for "
        "Tuesday, November 10, 2026"
    )
    last_title = "TransDigm Group Reports Fiscal 2026 Third Quarter Results"
    now = datetime(2026, 11, 10, 2, tzinfo=ET)
    cik = 1260221
    company = LUCID.model_copy(
        update={"name": "TransDigm Group INC", "cik": cik, "release_time": "07:15"}
    )
    released(
        f"{last_title}. /PRNewswire/ -- The webcast is at https://event.example.com. "
        "See the investor relations page of our website at https://www.transdigm.com."
    )
    chrome = FakeChrome(
        {
            home: (200, f'<a href="{investors}">Investors</a>'),
            investors: (
                200,
                f'<a href="{wrong}">News Releases</a><a href="{wrapper}">Press Releases</a>',
            ),
            wrong: (200, '<a href="/other">Company news</a>'),
            wrapper: (200, f'<iframe src="{hosted}?auth_token=abc-123"></iframe>'),
            hosted: (
                200,
                f'<a href="{notice}">{notice_title}</a><a href="{article}">{last_title}</a>',
            ),
            article: (200, f"<h1>{last_title}</h1><p>August 4, 2026 at 7:15 AM EDT</p>{REPORT}"),
            notice: (
                200,
                (
                    "<p>TransDigm Group said it will report fiscal 2026 fourth quarter earnings "
                    "before the market opens on Tuesday, November 10, 2026.</p>"
                ),
            ),
        }
    )
    fetcher = RoutedFetcher({"gcs-web.com/": (403, b""), home: (200, b"<html>")})
    monkeypatch.setattr(fetcher, "research_browser", chrome.open)
    monkeypatch.setattr(date_discovery, "chrome_available", lambda: True)
    monkeypatch.setattr(ir_research, "chrome_available", lambda: True)
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("TDG", company)
        calendar.save_review(cik, now, "Previous SEC-only source audit")
        dates = DateDiscovery(calendar, fetcher, lambda: now, lambda: False, lambda _: None)
        await dates.run_once(row)
        [planned] = calendar.entries()
        assert calendar.review(cik) == (None, None)

    assert planned.company.ir_listing == hosted
    assert planned.company.ir_release == article
    assert planned.company.ir_posted == datetime(2026, 8, 4, 7, 15, tzinfo=ET)
    assert (hosted, hosted) in chrome.calls
    assert not any("event.example.com" in url for url, _ in chrome.calls)
    assert planned.target is not None
    assert planned.target.expected == datetime(2026, 11, 10, 7, 15, tzinfo=ET)
    assert planned.target.period == "FY2026Q4"
    assert planned.target.ir and planned.target.ir.listing == hosted
    assert planned.evidence == f"Issuer date confirmed, before market open <{notice}>"


@pytest.mark.parametrize(
    ("text", "posted"),
    [
        ("Title\nAug 13, 2026 at 4:01 PM EDT\nPDF", datetime(2026, 8, 13, 16, 1, tzinfo=ET)),
        ("November 12, 2026 4:05 p.m. ET", datetime(2026, 11, 12, 16, 5, tzinfo=ET)),
        ("Feb. 3, 2027, 8:30 AM EST", datetime(2027, 2, 3, 8, 30, tzinfo=ET)),
        ("Oct 22, 2026 at 12:15 PM Eastern", datetime(2026, 10, 22, 12, 15, tzinfo=ET)),
        ("Aug 13, 2026", None),
        ("Feb 30, 2026 at 4:01 PM EDT", None),
    ],
)
def test_a_release_page_says_when_it_was_posted(text: str, posted: datetime | None) -> None:
    assert investigate.posted_time(text) == posted


async def test_an_unreadable_notice_on_the_press_release_list_is_reported() -> None:
    listing = "https://ir.example.com/news-releases"
    notice = "https://ir.example.com/news/q4-date"
    title = "Example to Announce Fourth Quarter Fiscal 2026 Results"
    fetcher = RoutedFetcher(
        {
            notice: (200, b"<p>Details to follow.</p>"),
            listing: (200, f'<a href="{notice}">{title}</a>'.encode()),
        }
    )
    company = LUCID.model_copy(update={"ir_listing": listing})
    unread: list[str] = []

    assert await date_discovery.find_date(fetcher, "LCID", NOW, company, unread) is None
    assert unread == [title]


class TwoCopies:
    """SEC's verified results 6-Ks for WeRide's Aug 12 release, newest first: the Hong Kong
    interim announcement, then the press release carrying the wire's dateline and IR site."""

    async def history(
        self, cik: int, now: datetime, count: int = 3, filings: object = None
    ) -> list[Sighting]:
        def copy(key: str, at: str, text: str) -> Sighting:
            return Sighting(key, "6-K financial results", "", at, True, body=text.encode())

        return [
            copy("b", "2026-08-12T20:03:22Z", "INTERIM RESULTS ANNOUNCEMENT. Revenue grew."),
            copy(
                "a",
                "2026-08-12T13:22:00Z",
                "NEW YORK, August 12, 2026 (GLOBE NEWSWIRE) -- WeRide Inc. today announced its "
                "results. Visit https://ir.weride.ai for more.",
            ),
            copy("c", "2026-05-13T10:16:36Z", "NEW YORK (PRNewswire) -- an older release"),
        ]


async def test_a_6k_filers_wire_and_ir_site_come_from_any_copy_of_its_latest_results() -> None:
    company = LUCID.model_copy(update={"sec_form": "6-K", "name": "WeRide Inc."})

    found = await investigate.investigate(
        RoutedFetcher({}), company, TODAY, None, sixk=cast(SixKResults, TwoCopies())
    )

    assert found.wire == "GlobeNewswire"
    assert "ir.weride.ai homepage over HTTP" in [check.source for check in found.checks]


def test_a_live_release_teaches_the_bot_its_wire() -> None:
    # Sea's 6-Ks name no wire, so both main wires were watched; Business Wire carried its Q3
    assert wire_seen(["edgar", "businesswire_busted", "prnewswire"]) == "Business Wire"
    assert wire_seen(["edgar", "q4_press"]) is None
    company = LUCID.model_copy(
        update={
            "wire": "unknown",
            "checks": [
                SourceCheck(source="SEC 6-K filings", watch=True, why="Filed here"),
                SourceCheck(source="PR Newswire earnings feed", watch=True, why="Both watched"),
            ],
        }
    )

    learned = with_wire(company, "Business Wire")

    target = to_target("SE", NOW, 2026, 3, learned)
    assert (target.newswire, target.prnewswire) == (learned.names, [])
    assert [(c.source, c.watch) for c in learned.checks] == [
        ("SEC 6-K filings", True),
        ("PR Newswire earnings feed", False),
        ("Business Wire earnings feed", True),
        ("GlobeNewswire earnings feed", False),
    ]


class OnReleases:
    """On's last three results 6-Ks, newest first. Only the Q2 one names its IR site."""

    def __init__(self, *, named: bool = True) -> None:
        self.named = named

    async def history(
        self, cik: int, now: datetime, count: int = 3, filings: object = None
    ) -> list[Sighting]:
        site = " Visit investors.on-running.com." if self.named else ""

        def copy(key: str, at: str, text: str) -> Sighting:
            return Sighting(key, "6-K financial results", "", at, True, body=text.encode())

        return [
            copy("q3", "2026-11-10T11:06:50Z", "On Reports Results for the Third Quarter"),
            copy("q2", "2026-08-11T10:10:30Z", f"On Reports Results for the Second Quarter.{site}"),
            copy("q1", "2026-05-12T10:10:27Z", "On Reports First Quarter 2026 Results"),
        ]


async def test_an_ir_site_named_by_an_earlier_release_is_still_found() -> None:
    # On's Q3 2026 release left out the IR site its Q2 release named
    company = LUCID.model_copy(update={"sec_form": "6-K", "name": "On Holding AG"})

    found = await investigate.investigate(
        RoutedFetcher({}), company, TODAY, None, sixk=cast(SixKResults, OnReleases())
    )

    assert "investors.on-running.com homepage over HTTP" in [c.source for c in found.checks]


async def test_earlier_research_stands_when_no_release_names_an_ir_site() -> None:
    company = LUCID.model_copy(
        update={
            "sec_form": "6-K",
            "name": "On Holding AG",
            "platform": "q4",
            "q4_host": "investors.on-running.com",
            "reachable": True,
        }
    )

    found = await investigate.investigate(
        RoutedFetcher({}), company, TODAY, None, sixk=cast(SixKResults, OnReleases(named=False))
    )

    assert (found.platform, found.q4_host, found.reachable) == (
        "q4",
        "investors.on-running.com",
        True,
    )
    assert ("Earlier research: investors.on-running.com", True) in [
        (c.source, c.watch) for c in found.checks
    ]


@pytest.mark.parametrize(
    ("releases", "year_end", "offset"),
    [
        (HD_RELEASES, "0131", -1),
        (LOW_RELEASES, "0131", -1),
        (TGT_RELEASES, "0201", -1),
        (NVDA_RELEASES, "0131", 0),
        (CRM_RELEASES, "0131", 0),
        (WMT_RELEASES, "0131", None),
    ],
    ids=["HD", "LOW", "TGT", "NVDA", "CRM", "WMT"],
)
async def test_the_investigation_learns_how_the_company_names_its_fiscal_years(
    monkeypatch: pytest.MonkeyPatch,
    releases: list[tuple[str, str]],
    year_end: str,
    offset: int | None,
) -> None:
    async def last_releases(fetcher: object, cik: int) -> list[tuple[Sighting, str]]:
        return sec_releases(releases)

    monkeypatch.setattr(investigate, "_last_releases", last_releases)
    company = LUCID.model_copy(update={"fiscal_year_end": year_end})

    found = await investigate.investigate(RoutedFetcher({}), company, TODAY, None)

    assert found.fiscal_year_offset == offset


async def test_what_earlier_research_learned_stands_when_the_releases_dont_say(
    released: Callable[[str], None],
) -> None:
    released(WMT_RELEASES[0][1])
    company = LUCID.model_copy(update={"fiscal_year_end": "0131", "fiscal_year_offset": -1})

    found = await investigate.investigate(RoutedFetcher({}), company, TODAY, None)

    assert found.fiscal_year_offset == -1
