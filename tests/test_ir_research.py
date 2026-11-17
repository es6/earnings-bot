import re
from datetime import date

import pytest

from first_print import ir_research
from first_print.browser_sources import IRSource
from first_print.ir_research import (
    issuer_links,
    matches_release,
    quarter_headline,
    verified_browser_source,
    verified_results_table,
)
from tests.fakes import RoutedFetcher


def test_inspector_requires_same_record_release_evidence() -> None:
    headline = "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027"
    day = date(2026, 8, 26)
    assert matches_release({"releases": [{"Headline": headline}]}, day, {headline})
    assert matches_release(
        [{"title": headline, "publish_date": "2026-08-26T20:20:00Z"}], day, set()
    )
    assert not matches_release(
        [{"title": headline}, {"publish_date": "2026-08-26T20:20:00Z"}], day, set()
    )
    assert not matches_release(
        [{"title": "NVIDIA Sets Conference Call for Earnings", "publish_date": "2026-08-26"}],
        day,
        set(),
    )


def test_issuer_listing_keeps_same_origin_links() -> None:
    links = issuer_links(
        "https://ir.crowdstrike.com/press-releases",
        b'<a href="/news/release">Quarterly results</a>'
        b'<a href="https://elsewhere.example/news/release">Other results</a>',
    )
    assert links == [("https://ir.crowdstrike.com/news/release", "Quarterly results")]


def test_browser_headline_requires_the_scheduled_fiscal_period() -> None:
    pattern = re.compile(quarter_headline(2027, 3), re.IGNORECASE)
    assert pattern.fullmatch("CrowdStrike Reports Third Quarter Fiscal Year 2027 Financial Results")
    assert pattern.fullmatch("CrowdStrike Reports Q3 FY27 Results")
    assert not pattern.fullmatch("CrowdStrike Announces Date of Fiscal Third Quarter 2027 Results")
    assert not pattern.fullmatch("CrowdStrike Reports Second Quarter Fiscal Year 2027 Results")
    assert not pattern.fullmatch("CrowdStrike Reports Third Quarter Fiscal Year 2026 Results")


def test_a_results_headline_that_announces_rather_than_reports_counts() -> None:
    pattern = re.compile(quarter_headline(2026, 2), re.IGNORECASE)
    assert pattern.fullmatch("Lucid Announces Operational Reset and Second Quarter 2026 Results")
    assert not pattern.fullmatch(
        "Lucid Announces Q2 Production and Deliveries, Sets Date for Second Quarter 2026 Earnings"
    )
    assert not pattern.fullmatch("Lucid Announces Second Quarter 2026 Results Conference Call")


LISTING = "https://ir.lucidmotors.com/press-releases"
Q2 = "https://ir.lucidmotors.com/news-releases/q2"
Q2_TITLE = "Lucid Announces Operational Reset and Second Quarter 2026 Results"
REPORT = (
    f"<h1>{Q2_TITLE}</h1><p>Revenue was $259 million. Net loss per share was $0.24.</p>"
    + "<p>Details of the quarter.</p>" * 200
)


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ir_research, "chrome_available", lambda: True)


VERIFIED_SOURCE = IRSource(listing=LISTING, headline=quarter_headline(2026, 3), verify_text=True)


@pytest.mark.usefixtures("chrome")
@pytest.mark.parametrize(
    ("routes", "why", "expected"),
    [
        (
            {LISTING: (200, f'<a href="{Q2}">{Q2_TITLE}</a>'), Q2: (200, REPORT)},
            f"Last quarter's results, “{Q2_TITLE}”, read in Chrome",
            VERIFIED_SOURCE,
        ),
        ({LISTING: (403, "Access Denied")}, f"Chrome got HTTP 403 from <{LISTING}>", None),
        (
            {LISTING: (200, '<a href="/news/day">Lucid to Host Investor Day</a>')},
            f"No Q2 2026 results headline on <{LISTING}> to check the pattern against",
            None,
        ),
        (
            {LISTING: (200, f'<a href="{Q2}">{Q2_TITLE}</a>'), Q2: (200, "<p>Coming soon</p>")},
            f"Last quarter's article “{Q2_TITLE}” didn't read as a results report",
            None,
        ),
    ],
    ids=["verified", "blocked", "no-headline", "not-a-report"],
)
async def test_a_live_chrome_source_is_verified_or_says_why_not(
    routes: dict[str, tuple[int, str]], why: str, expected: IRSource | None
) -> None:
    fetcher = RoutedFetcher(
        {url: (status, body.encode()) for url, (status, body) in routes.items()}
    )

    source, reason = await verified_browser_source(fetcher, LISTING, "FY2026Q3")

    assert reason == why
    assert source == expected


@pytest.mark.usefixtures("chrome")
@pytest.mark.parametrize("chrome_allowed", [True, False], ids=["allowed", "kept-for-live-windows"])
async def test_research_opens_its_own_chrome_only_when_live_windows_leave_it_free(
    chrome_allowed: bool,
) -> None:
    fetcher = RoutedFetcher({})

    with fetcher.background(chrome=chrome_allowed):
        inspected = await ir_research.inspect_issuer(fetcher, LISTING, date(2026, 8, 26), set())
        table = await ir_research.verified_results_table(fetcher, LISTING, "FY2026Q3")

    assert table == {}
    assert fetcher.chrome_opened == (2 if chrome_allowed else 0)
    assert bool(inspected) is chrome_allowed


async def test_no_chrome_or_no_page_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ir_research, "chrome_available", lambda: False)
    assert await verified_browser_source(RoutedFetcher({}), LISTING, "FY2026Q3") == (
        None,
        "Chrome isn't available on this computer",
    )
    monkeypatch.setattr(ir_research, "chrome_available", lambda: True)
    assert await verified_browser_source(RoutedFetcher({}), None, "FY2026Q3") == (
        None,
        "No press release page found for it",
    )


async def test_a_prior_fiscal_year_table_cannot_arm_the_next_year() -> None:
    page = "https://issuer.example/financials/2026"
    pdf = "https://issuer.example/Q4-2026-ER.pdf"
    body = b"""<table><tr><th></th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th></tr>
        <tr><td>Earnings Release</td><td></td><td></td><td></td>
            <td><a href="/Q4-2026-ER.pdf">PDF</a></td></tr></table>"""
    fetcher = RoutedFetcher({page: (200, body), pdf: (200, b"%PDF-1.6")})

    assert await verified_results_table(fetcher, page, "FY2027Q1") == {}
    assert not fetcher.requests
