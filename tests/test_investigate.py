"""The bot's own investigation of a company it has no research on."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Self

import pytest

from earnings_bot import date_discovery, investigate
from earnings_bot.calendar import Calendar, describe
from earnings_bot.date_discovery import DateDiscovery
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.investigate import due, feed_links, ir_hosts, wire
from earnings_bot.issuers import profile
from earnings_bot.known import known_companies, to_target
from earnings_bot.recorder import ET
from tests.fakes import RoutedFetcher
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS
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
            LUCID_Q2: (200, f"<h1>Second Quarter 2026 Results</h1><p>{DATELINE}</p>"),
        }
    )


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Sets the text of the company's last results release, as SEC has it."""

    def set_text(text: str) -> None:
        async def last_release(fetcher: object, cik: int) -> str:
            return text

        monkeypatch.setattr(investigate, "_last_release", last_release)

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
    assert chrome.started == 0
    target = to_target("LCID", NOW, 2026, 4, found)
    assert [s.name for s in target.sources()] == [
        "q4_press",
        "q4_files",
        "edgar",
        "edgar_latest",
        "businesswire_busted",
    ]


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
    # One Chrome session: the site, its press release list, then its latest results
    assert chrome.started == 1
    assert chrome.calls == [
        (LUCID_HOME, LUCID_HOME),
        (LUCID_LIST, LUCID_HOME),
        (LUCID_Q2, LUCID_HOME),
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
        "either. Wire not found, so both are watched."
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
        "Investigated Nov 09: No IR site found in its last results release. Wire: Business Wire."
    )


async def test_a_globenewswire_company_is_matched_there_on_trial(
    released: Callable[[str], None],
) -> None:
    released("SANTA CLARA, Calif., Nov. 3, 2026 (GLOBE NEWSWIRE) -- Example today reported")

    found = await investigate.investigate(RoutedFetcher({}), LUCID, TODAY, None)

    target = to_target("LCID", NOW, 2026, 3, found)
    assert (target.newswire, target.prnewswire) == ([], [])
    assert target.trial.globenewswire == LUCID.names
    assert "globenewswire (trial)" in [
        s.name + (" (trial)" if s.trial else "") for s in target.sources()
    ]


async def test_the_last_release_is_read_from_the_results_8k() -> None:
    # Newest first: a shareholder vote 8-K and a 10-Q were filed after the results
    filings = [
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

    text = await investigate._last_release(fetcher, 1543151)

    assert text is not None and "Gross bookings grew 21%" in text
    # Only the results 8-K is read
    assert not any("000154315126000041" in url for _, url, _ in fetcher.requests)
    assert not any("000154315126000042" in url for _, url, _ in fetcher.requests)


def test_only_companies_outside_the_catalog_are_investigated_and_then_monthly() -> None:
    assert not due("NVDA", known_companies()["NVDA"], TODAY)
    assert due("LCID", LUCID, TODAY)
    looked = LUCID.model_copy(update={"investigated": date(2026, 10, 11)})
    assert not due("LCID", looked, TODAY)
    assert due("LCID", looked, date(2026, 11, 10))


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
    async def nothing(*args: object) -> None:
        return None

    monkeypatch.setattr(date_discovery, "verified_browser_source", nothing)
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
        calendar.add("LCID", 1, NOW, LUCID)
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
        assert statuses == [f"LCID: {row.company.evidence}", describe(row.target, row.evidence)]
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
        calendar.add("LCID", 1, NOW, LUCID)
        dates = DateDiscovery(
            calendar, fetcher, lambda: NOW, lambda: window_open, statuses.append, None, chrome.open
        )
        [row] = calendar.entries()

        await dates._check(row)

        [after] = calendar.entries()
        assert after.company == LUCID
    assert statuses == []
