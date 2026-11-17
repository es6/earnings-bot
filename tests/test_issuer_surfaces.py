import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from first_print import investigate
from first_print.fetch import Response
from first_print.ir_research import quarter_headline
from first_print.issuer_history import listing_history, listing_rows
from first_print.issuer_news import IssuerNews, discover_manifest, news_records
from first_print.known import known_companies
from first_print.recorder import ET
from first_print.source_audit import release_history
from first_print.sources import Sighting
from tests.fakes import RoutedFetcher

FIX = Path(__file__).parent / "fixtures" / "issuer-history"
ROAD = "https://ir.constructionpartners.net/news/press-releases"
JPM = "https://www.jpmorganchase.com"
API = JPM + "/services/json/v1/investor-relations/quarterly-earnings.json"


async def test_public_pagination_recovers_road_history_without_a_fixed_clock_exception() -> None:
    routes = {
        ROAD + (f"?page={n}" if n > 1 else ""): (200, (FIX / f"road-page{n}.html").read_bytes())
        for n in range(3, 0, -1)
    }
    fetcher = RoutedFetcher(routes)
    company = known_companies()["NVDA"].model_copy(
        update={
            "name": "Construction Partners Inc",
            "names": ["Construction Partners"],
            "cik": 1718227,
            "q4_host": None,
            "wire": "PR Newswire",
            "ir_listing": ROAD,
        }
    )
    filed = [
        datetime(*day, 9, tzinfo=ET)
        for day in [
            (2026, 11, 5),
            (2026, 8, 7),
            (2026, 5, 8),
            (2026, 2, 5),
            (2025, 11, 20),
            (2025, 8, 7),
            (2025, 5, 9),
        ]
    ]
    history = await release_history(fetcher, company, [], filed)
    assert len(history) == 6
    assert history[2] == datetime(2026, 2, 5, 7, 30, tzinfo=ET)
    assert {value.strftime("%H:%M") for value in history} == {"07:00", "07:30"}
    assert not any(value.month == 11 and value.day == 5 for value in history)


async def test_history_follows_only_bounded_linked_pages_and_same_host() -> None:
    source = (
        '<article><time datetime="2026-02-05T07:30:00">Feb 5 7:30 EST</time>'
        '<a href="/q1">Example announces first quarter results</a></article>'
    )
    rows, next_url = listing_rows(
        ROAD, (source + '<a rel="next" href="https://other.example/next">Next</a>').encode()
    )
    assert len(rows) == 1 and next_url is None
    assert listing_rows(ROAD, source.replace("EST", "EDT").encode())[0] == []
    fetcher = RoutedFetcher(
        {
            ROAD: (200, (source + '<a rel="next" href="?page=1">Next</a>').encode()),
            ROAD + "?page=1": (200, source.encode()),
        }
    )
    assert len(await listing_history(fetcher, ROAD)) == 1  # duplicate release is one identity


async def test_history_reads_at_most_three_linked_pages() -> None:
    days = ["2026-02-05", "2026-05-07", "2026-08-06", "2026-11-05"]
    pages = {}
    for n, day in enumerate(days):
        url = ROAD + (f"?page={n}" if n else "")
        row = (
            f'<article><time datetime="{day}T07:30:00-05:00">{day}</time>'
            f'<a href="/q{n}">Example announces quarter {n} results</a></article>'
        )
        link = f'<a rel="next" href="?page={n + 1}">Next</a>' if n + 1 < len(days) else ""
        pages[url] = (200, (row + link).encode())
    fetcher = RoutedFetcher(dict(reversed(pages.items())))  # a route matches by substring

    history = await listing_history(fetcher, ROAD)

    assert len(history) == 3
    assert [url for _, url, _ in fetcher.requests] == [ROAD, ROAD + "?page=1", ROAD + "?page=2"]


LUCID_FEED = (
    b"<rss><channel><item><title>Lucid reports third quarter 2026 "
    b"financial results</title>"
    b"<link>https://ir.lucidmotors.com/q3</link><guid>q3</guid>"
    b"<pubDate>Thu, 05 Nov 2026 16:05:00 -0500</pubDate></item></channel></rss>"
)


class BrowserFetcher(RoutedFetcher):
    """A fetcher whose owned browser answers `reply`, counting how often it is asked."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__({})
        self.reply = (status, body)
        self.browser_asked = 0

    async def browser_request(self, url: str, warmup: str, *args: Any, **kwargs: Any) -> Response:
        self.browser_asked += 1
        return Response(url, self.reply[0], {}, self.reply[1], 0)


@pytest.mark.parametrize(
    ("status", "body", "chrome", "history", "asked"),
    [
        pytest.param(
            200, LUCID_FEED, True, [datetime(2026, 11, 5, 16, 5, tzinfo=ET)], 1, id="readable"
        ),
        pytest.param(403, b"Access Denied", True, [], 1, id="refused"),
        pytest.param(200, b"<rss><channel><item><title>", True, [], 1, id="malformed"),
        pytest.param(200, LUCID_FEED, False, [], 0, id="browser-held-for-live-windows"),
    ],
)
async def test_release_history_reads_a_feed_that_requires_the_owned_browser(
    status: int, body: bytes, chrome: bool, history: list[datetime], asked: int
) -> None:
    company = known_companies()["NVDA"].model_copy(
        update={
            "ir_listing": None,
            "ir_browser_rss": "https://ir.lucidmotors.com/rss",
            "q4_host": None,
            "wire": "none",
            "name": "Lucid Group",
            "cik": 1811210,
        }
    )
    fetcher = BrowserFetcher(status, body)

    with fetcher.background(chrome=chrome):
        result = await release_history(fetcher, company, [], [datetime(2026, 11, 5, 17, tzinfo=ET)])

    assert result == history
    assert fetcher.browser_asked == asked


async def test_exact_publisher_listing_identifies_a_wire_missing_from_the_exhibit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = known_companies()["NVDA"]
    filing = Sighting("8k", "8-K", "https://sec.gov/8k", "2026-08-27T20:27:00+00:00")
    release = "NVIDIA Reports Financial Results for Second Quarter Fiscal 2027"

    async def history(*args: Any) -> list[Sighting]:
        return [
            Sighting(
                "wire", release, "https://www.globenewswire.com/report", "2026-08-27T16:20:00-04:00"
            )
        ]

    monkeypatch.setattr(investigate, "past_releases", history)
    assert (
        await investigate.listed_wire(RoutedFetcher({}), company, [(filing, release)])
        == "GlobeNewswire"
    )
    old_filing = Sighting("old", "8-K", "https://sec.gov/old", "2026-05-27T20:27:00+00:00")
    assert (
        await investigate.listed_wire(RoutedFetcher({}), company, [(old_filing, release)]) is None
    )


async def test_jpm_manifest_discovers_and_verifies_the_real_random_pdf() -> None:
    payload = json.loads((FIX / "jpm-quarterly.json").read_text())
    records = [
        row for row in news_records(API, json.dumps(payload).encode()) if row.period == (2026, 3)
    ]
    assert len(records) == 1 and records[0].period == (2026, 3)
    assert "6c1f8114" in records[0].url and "Presentation" not in records[0].title
    pdf = (FIX / "jpm-q3-2026.pdf").read_bytes()
    fetcher = RoutedFetcher({API: (200, json.dumps(payload).encode()), records[0].url: (200, pdf)})
    source = IssuerNews(API, quarter_headline(2026, 3))
    assert source.parse(await source.fetch(fetcher))[0].body == pdf
    wrong = copy.deepcopy(payload)
    wrong["items"][0]["year"] = "2025"
    other_fetcher = RoutedFetcher(
        {API: (200, json.dumps(wrong).encode()), records[0].url: (200, pdf)}
    )
    wrong_source = IssuerNews(API, quarter_headline(2025, 3))
    assert wrong_source.parse(await wrong_source.fetch(other_fetcher)) == []
    # A manifest's role and label must agree; an unrelated PDF host or duplicate row is refused.
    presentation = copy.deepcopy(payload)
    presentation["items"][0]["docs"]["pressRelease"] = presentation["items"][0]["docs"][
        "presentation"
    ]
    assert not any(
        row.period == (2026, 3) for row in news_records(API, json.dumps(presentation).encode())
    )
    external = copy.deepcopy(payload)
    external["items"][0]["docs"]["pressRelease"]["link"] = "https://other.example/a.pdf"
    assert not any(
        row.period == (2026, 3) for row in news_records(API, json.dumps(external).encode())
    )
    ambiguous = copy.deepcopy(payload)
    second = copy.deepcopy(ambiguous["items"][0])
    second["docs"]["pressRelease"]["link"] = JPM + "/different.pdf"
    ambiguous["items"].append(second)
    assert not any(
        row.period == (2026, 3) for row in news_records(API, json.dumps(ambiguous).encode())
    )


async def test_manifest_discovery_requires_the_pages_declaration_and_linked_script() -> None:
    page = JPM + "/ir/quarterly-earnings"
    script = JPM + "/clientlib-site.js"
    html = (
        '<div class="ir-feed-main" data-feedtype="quarterly-earnings"></div>'
        '<script src="/clientlib-site.js"></script>'
    )
    payload = (FIX / "jpm-quarterly.json").read_bytes()
    fetcher = RoutedFetcher(
        {
            page: (200, html.encode()),
            script: (200, b'fetch("/services/json/v1/investor-relations/"+t+".json")'),
            API: (200, payload),
        }
    )
    assert await discover_manifest(fetcher, page) == API
    missing = RoutedFetcher(
        {page: (200, html.replace('data-feedtype="quarterly-earnings"', "").encode())}
    )
    assert await discover_manifest(missing, page) is None


@pytest.mark.parametrize(
    ("declaring", "found"),
    [
        pytest.param("/second.js", True, id="declared-by-the-second-script"),
        pytest.param("/third.js", False, id="declared-only-by-a-third-script"),
    ],
)
async def test_manifest_discovery_reads_at_most_two_scripts(declaring: str, found: bool) -> None:
    page = JPM + "/ir/quarterly-earnings"
    scripts = ["/first.js", "/second.js", "/third.js"]
    html = '<div class="ir-feed-main" data-feedtype="quarterly-earnings"></div>' + "".join(
        f'<script src="{src}"></script>' for src in scripts
    )
    routes = {page: (200, html.encode()), API: (200, (FIX / "jpm-quarterly.json").read_bytes())}
    for src in scripts:
        code = (
            b'fetch("/services/json/v1/investor-relations/"+t+".json")' if src == declaring else b""
        )
        routes[JPM + src] = (200, code)

    assert (await discover_manifest(RoutedFetcher(routes), page) == API) is found
