import asyncio
import io
import json
from datetime import datetime, timedelta

import pytest

from earnings_bot import recorder
from earnings_bot.recorder import ET, Journal, Polls, Target, Watch, follow_up
from earnings_bot.sec_results import (
    SixKResults,
    announces_results,
    is_results_release,
    reported_period,
)
from earnings_bot.sources import EdgarFilings, EdgarLatest, Sighting, _filing_type
from tests.brookfield_data import CIK, ORDINARY, RESULT, filings, fixtures, folder
from tests.builders import sec_filing_detail
from tests.fakes import PinnedClock, RoutedFetcher
from tests.results_data import (
    CRM_RELEASES,
    HD_RELEASES,
    LOW_RELEASES,
    NVDA_RELEASES,
    TGT_RELEASES,
    WMT_RELEASES,
)

NOW = datetime(2026, 11, 9, 17, tzinfo=ET)


@pytest.mark.parametrize(
    "description",
    ["Press Release", "Exhibit 99.1", "Quarterly Results"],
)
def test_financial_boilerplate_cannot_turn_a_6k_into_results(description: str) -> None:
    text = "Notes financing closed today.\n" + "Revenues and net income. " * 110
    assert not is_results_release(text, description)
    notice = (
        "Company Announces Date of Third Quarter Results\n"
        "The company reported earnings for the last quarter.\n" + text
    )
    assert not is_results_release(notice, description)
    assert not is_results_release(
        "Company previously reported second quarter 2026 results. Revenue was $1 billion.\n" + text,
        description,
    )


async def test_both_sec_watchers_verify_once_and_alert_only_on_results() -> None:
    journal = Journal(io.StringIO())
    now = journal.now()
    data = filings(accepted=now.isoformat())
    entries = "".join(
        f"<entry><id>urn:accno:{key}</id><title>6-K - Brookfield</title>"
        f'<link href="https://www.sec.gov{folder(key)}/{key}-index.htm"/>'
        f"<updated>{now.isoformat()}</updated><summary>Form 6-K</summary></entry>"
        for key in (ORDINARY, RESULT)
    )
    routes = fixtures(data)
    routes["type=6-K"] = (
        200,
        f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode(),
    )
    fetcher = RoutedFetcher(routes)
    target = Target(ticker="BN", cik=CIK, expected=now, sec_form="6-K")
    submissions, latest = target.sources()
    assert isinstance(submissions, EdgarFilings) and isinstance(latest, EdgarLatest)
    assert "type=6-K" in latest.url()
    end = now + timedelta(seconds=0.35)
    window = (now - timedelta(seconds=1), end)
    polls = Polls(fetcher, journal)
    seen: list[Sighting] = []
    await asyncio.gather(
        *(
            polls.watch(Watch("BN", source, window), end, lambda _, item: seen.append(item))
            for source in (submissions, latest)
        )
    )
    assert [(item.key, item.earnings, item.caught_up) for item in seen] == [
        (RESULT, True, True),
        (RESULT, True, True),
    ]
    urls = [url for _, url, _ in fetcher.requests]
    for key in (ORDINARY, RESULT):
        assert sum(url.endswith(f"{folder(key)}/exh_991.htm") for url in urls) == 1


# WeRide's results 6-Ks of Aug 12, 2026 open with the Hong Kong exchange's disclaimers
HKEX_DISCLAIMER = (
    "Hong Kong Exchanges and Clearing Limited and The Stock Exchange of Hong Kong Limited take "
    "no responsibility for the contents of this announcement, make no representation as to its "
    "accuracy or completeness and expressly disclaim any liability whatsoever for any loss "
    "howsoever arising from or in reliance upon the whole or any part of the contents of this "
    "announcement. "
)
FIGURES = [
    "Total revenue | 231.7 | 127.2",
    "Revenue increased by 82.2% to RMB231.7 million. Net loss per share was RMB0.92.",
] * 30
# The press release itself, as Schedule I after the exchange's announcement
SCHEDULE = "\n".join(
    [
        "Exhibit 99.1",
        HKEX_DISCLAIMER * 4 + "This announcement contains forward-looking statements. " * 20,
        "Schedule I",
        "WeRide Inc. Announces Second Quarter and First Half 2026 Unaudited Financial Results",
        (
            "NEW YORK, August 12, 2026 (GLOBE NEWSWIRE) - WeRide Inc. today announced its "
            "unaudited financial results for the second quarter and first half ended June 30, 2026."
        ),
        *FIGURES,
    ]
)
# The exchange's own interim results announcement, filed the same afternoon
INTERIM = "\n".join(
    [
        "Exhibit 99.1",
        HKEX_DISCLAIMER,
        "INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED JUNE 30, 2026",
        (
            "The Company is pleased to announce the unaudited consolidated quarterly and first "
            "half results of the Company for the three months and first half ended June 30, 2026."
        ),
        *FIGURES,
    ]
)


def test_a_press_release_after_the_exchanges_disclaimers_is_recognized_as_results() -> None:
    assert SCHEDULE.index("today announced") > 2500
    assert is_results_release(SCHEDULE, "EXHIBIT 99.1")


def test_pleased_to_announce_results_is_results_and_not_a_date_notice() -> None:
    assert is_results_release(INTERIM, "EXHIBIT 99.1")
    notice = INTERIM.replace("is pleased to announce the", "will announce its")
    assert not is_results_release(notice, "EXHIBIT 99.1")


def test_reports_third_quarter_results_is_an_announcement() -> None:
    # CGI's press release, the third exhibit of its results 6-K
    release = "\n".join(
        [
            "GLOBAL PRESS RELEASE | Stock Market Symbols | GIB.A (TSX) | GIB (NYSE)",
            "CGI reports third quarter Fiscal 2026 results",
            "Revenue up 2.5% and diluted EPS accretion of 22.5%",
            *FIGURES,
        ]
    )
    assert is_results_release(release, "EX-99.3")
    notice = release.replace("CGI reports third quarter", "CGI to host third quarter")
    assert not is_results_release(notice, "EX-99.3")


async def test_a_results_6k_can_begin_after_a_full_cover_page() -> None:
    cover = "UNITED STATES\nSECURITIES AND EXCHANGE COMMISSION\nFORM 6-K\n"
    cover += "Cover page item\n" * 15
    report = (
        "Company Reports Second Quarter 2026 Financial Results\n"
        "The company announced its financial results for the quarter ended June 30, 2026.\n"
        + "Revenue of $4 billion and net income of $300 million.\n"
        * 55
    )
    url = "https://www.sec.gov" + folder(RESULT)
    filename = f"{url}/cover.htm"
    filing = Sighting(RESULT, "6-K", filename, details={"form": "6-K"})
    fetcher = RoutedFetcher({f"{url}/{RESULT}-index.htm": (200, b"Document Format Files")})
    unexpected = cover + "Yes No (X)\nNon-results disclosure\n" + "Cover page item\n" * 20 + report
    for body, expected in ((unexpected, False), (cover + "Yes No (X)\n" + report, True)):
        fetcher.routes[filename] = (
            200,
            "".join(f"<p>{line}</p>" for line in body.splitlines()).encode(),
        )
        assert bool(await SixKResults(fetcher).verify(filing)) is expected


async def test_followup_verifies_a_results_6k_accepted_after_the_window() -> None:
    journal = Journal(io.StringIO(), PinnedClock())
    now = journal.now()
    target = Target(
        ticker="BN",
        cik=CIK,
        expected=now - timedelta(minutes=1),
        before_minutes=2,
        after_minutes=0,
        sec_form="6-K",
        period="FY2026Q2",
    )
    fetcher = RoutedFetcher(fixtures(filings(accepted=now.isoformat())))
    # An uninitialized watcher also happens when the bot resumes a delivered alert.
    watch_state = Watch("BN", EdgarFilings(CIK, form="6-K"), target.window, period=target.period)
    seen: list[Sighting] = []

    await follow_up(
        target,
        [watch_state],
        fetcher,
        journal,
        lambda _, __, item: seen.append(item),
        SixKResults(fetcher),
        longest=timedelta(minutes=1, milliseconds=100),
    )

    assert [item.key for item in seen if item.earnings] == [RESULT]


async def test_after_midnight_6k_is_verified_and_catches_up_inside_the_window() -> None:
    start = datetime(2026, 11, 11, 23, 55, tzinfo=ET)
    accepted = start + timedelta(minutes=10)
    source = EdgarFilings(CIK, form="6-K")
    w = Watch("BN", source, (start, start + timedelta(minutes=40)), period="FY2026Q2")
    fetcher = RoutedFetcher(fixtures(filings(accepted=accepted.isoformat())))
    journal = Journal(io.StringIO())
    response = await fetcher.request("GET", source.url())
    items = await recorder._verify_sixks(w, response, SixKResults(fetcher), journal)
    sightings: list[Sighting] = []

    recorder.record_items(
        w, items, response, journal, accepted, lambda _, item: sightings.append(item)
    )

    assert [item.key for item in sightings if item.earnings and item.caught_up] == [RESULT]


@pytest.mark.parametrize(
    ("releases", "periods"),
    [
        # Fiscal years named for the year they start in
        (HD_RELEASES, [(2026, 2), (2026, 1), (2025, 4)]),
        (LOW_RELEASES, [(2026, 2), (2026, 1), (2025, 4)]),
        # From the dateline, since the headline names no year. Not "compared with Q1 2025"
        (TGT_RELEASES, [(2026, 2), (2026, 1), (2025, 4)]),
        # Named for the year they end in
        (NVDA_RELEASES, [(2027, 2), (2027, 1), (2026, 4)]),
        # "Salesforce Delivers Record Fourth Quarter Fiscal 2026 Results" is a headline line,
        # though "delivers" isn't how results are usually announced
        (CRM_RELEASES, [(2027, 2), (2027, 1), (2026, 4)]),
        # Not its outlook "for Q1 and FY27"
        (WMT_RELEASES, [None, None, None]),
    ],
    ids=["HD", "LOW", "TGT", "NVDA", "CRM", "WMT"],
)
def test_a_results_release_names_the_period_it_reports(
    releases: list[tuple[str, str]], periods: list[tuple[int, int] | None]
) -> None:
    assert [reported_period(text) for _, text in releases] == periods


@pytest.mark.parametrize(
    ("opening", "period"),
    [
        # Ollie's headline, split across two lines, Sep 2, 2026
        (
            (
                "Exhibit 99.1\nOllie\u2019s Bargain Outlet Holdings, Inc. Announces\n"
                "Second Quarter Fiscal 2026 Results\nNet Sales Increased 9.1%"
            ),
            (2026, 2),
        ),
        # Williams-Sonoma's, Aug 26, 2026
        (
            (
                "Exhibit 99.1\n"
                "Williams-Sonoma, Inc. announces strong second quarter 2026 results\n"
                "Q2 comparable brand revenue +6.2%"
            ),
            (2026, 2),
        ),
        # A date notice isn't a results headline
        ("Acme to Report Third Quarter 2026 Results on November 12", None),
    ],
)
def test_a_headline_line_names_the_period_too(opening: str, period: tuple[int, int] | None) -> None:
    assert reported_period(opening) == period


def test_a_comparison_with_last_year_isnt_the_period_reported() -> None:
    # Lowe's dateline without its headline: "today reported net earnings ... for the quarter
    # ended July 31, 2026, compared to diluted EPS of $4.27 in the second quarter of 2025". The
    # quarter it names is last year's, too far past the announcement to count.
    [dateline] = [line for line in LOW_RELEASES[0][1].splitlines() if "today reported" in line]
    assert reported_period(dateline) is None


BAM = 1937926
BAM_RESULT, BAM_DIVIDEND = "0001171843-26-007210", "0001171843-26-007300"
BAM_RELEASE = (
    "<h1>Brookfield Asset Management Reports Third Quarter 2026 Results</h1>"
    "<p>NEW YORK, Nov. 6, 2026 (GLOBE NEWSWIRE) -- Brookfield Asset Management Ltd. today "
    "announced financial results for the quarter ended September 30, 2026.</p>"
    + "<p>Revenues of $1.2 billion. Fee-related earnings of $750 million and net income of "
    "$600 million. </p>" * 35
)
BAM_DIVIDEND_NOTICE = (
    "<h1>Brookfield Asset Management Declares Quarterly Dividend</h1>"
    + "<p>The board declared a quarterly dividend of $0.4375 per share.</p>" * 40
    + "<p>Revenues of $1 billion. Net income grew.</p>"
)


async def test_an_8k_under_other_events_alerts_only_when_its_exhibit_is_results() -> None:
    """Brookfield Asset Management furnishes results, dividends and deals all as 8-K Item 8.01
    exhibits; only the results one may alert."""
    journal = Journal(io.StringIO())
    now = journal.now()
    accepted = now.isoformat()
    data = {
        "filings": {
            "recent": {
                "accessionNumber": [BAM_DIVIDEND, BAM_RESULT],
                "form": ["8-K", "8-K"],
                "items": ["8.01,9.01", "8.01,9.01"],
                "acceptanceDateTime": [accepted, accepted],
                "primaryDocument": ["f8k.htm", "f8k.htm"],
            }
        }
    }
    routes: dict[str, tuple[int, bytes]] = {f"CIK{BAM:010d}.json": (200, json.dumps(data).encode())}
    entries = ""
    for key, html in [(BAM_DIVIDEND, BAM_DIVIDEND_NOTICE), (BAM_RESULT, BAM_RELEASE)]:
        base = f"/Archives/edgar/data/{BAM}/{key.replace('-', '')}"
        routes[f"{base}/{key}-index.htm"] = (200, sec_filing_detail(f"{base}/f8k.htm", "ex991.htm"))
        routes[f"{base}/ex991.htm"] = (200, html.encode())
        entries += (
            f"<entry><id>urn:accno:{key}</id><title>8-K - Brookfield Asset Management</title>"
            f'<link href="https://www.sec.gov{base}/{key}-index.htm"/><updated>{accepted}</updated>'
            "<summary>Item 8.01: Other Events Item 9.01: Financial Statements and Exhibits"
            "</summary></entry>"
        )
    routes["type=8-K"] = (
        200,
        f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode(),
    )
    fetcher = RoutedFetcher(routes)
    target = Target(ticker="BAM", cik=BAM, expected=now)
    submissions, latest = target.sources()
    end = now + timedelta(seconds=0.35)
    window = (now - timedelta(seconds=1), end)
    polls = Polls(fetcher, journal)
    seen: list[Sighting] = []
    await asyncio.gather(
        *(
            polls.watch(Watch("BAM", source, window), end, lambda _, item: seen.append(item))
            for source in (submissions, latest)
        )
    )

    assert {(item.key, item.earnings, item.title) for item in seen} == {
        (BAM_RESULT, True, "8-K financial results")
    }
    # The release text is the verified exhibit, not the 8-K cover
    assert all(item.body and b"Fee-related earnings" in item.body for item in seen)


def test_an_8k_under_results_of_operations_needs_no_exhibit_check() -> None:
    assert _filing_type("8-K", "2.02,9.01") == (True, {})
    assert _filing_type("8-K", "8.01") == (False, {})  # no exhibit: nothing to read
    assert _filing_type("8-K", "5.02") == (False, {})


# The opening of Klarna's Feb 19 release as its IR site carries it: a short version, with no net
# income or EPS, under a headline that doesn't read as earnings
KLARNA_Q4 = (
    "Klarna Accelerates U.S. Growth and Delivers $1bn Revenue Driven by Rapid Banking Service "
    "Adoption\n"
    "NEW YORK--(BUSINESS WIRE)-- Klarna, the global digital bank and flexible payments provider, "
    "today reported fourth quarter 2025 results that demonstrate accelerating momentum. The "
    "company delivered its first billion-dollar revenue quarter, with GMV reaching $38.7 billion, "
    "while revenue grew 38% to $1.082 billion, also beating guidance.\n"
    '"We\'ve been executing on a clear plan," said the chief executive.\n'
)
# GlobeNewswire's page for WeRide's Nov 24, 2025 release, read as text: the site's navigation
# first, then the story
WERIDE_Q3 = (
    "Accessibility: Skip TopNav\n"
    + "".join(f"Industry News {n}\n" for n in range(46))
    + "Global Expansion Accelerates: Landmark UAE Driverless Robotaxi Commercial Permit, Record "
    "YoY Revenue Growth of 144.3%\n"
    "WeRide Announces Unaudited Financial Results for Q3 2025\n"
    "November 24, 2025 04:00 ET | Source: WeRide Inc.\n"
    "NEW YORK, Nov. 24, 2025 (GLOBE NEWSWIRE) -- WeRide Inc. (Nasdaq: WRD, HKEX:0800) today "
    "announced its unaudited financial results for the three months ended September 30, 2025.\n"
    "Financial and Operational Highlights\n"
    "Total revenue grew 144.3% year-over-year to RMB171.0 million (US$24.0 million) in 3Q2025.\n"
)


def test_a_release_under_a_headline_that_isnt_earnings_is_known_by_its_text() -> None:
    assert announces_results(KLARNA_Q4, "FY2025Q4")
    assert not is_results_release(KLARNA_Q4, "")  # it asks for net income or EPS
    assert announces_results(WERIDE_Q3, "FY2025Q3")
    assert not is_results_release(WERIDE_Q3, "")  # the navigation hides the announcement


def test_a_text_check_wants_this_quarters_results_with_revenue_figures() -> None:
    assert not announces_results(KLARNA_Q4, "FY2026Q1")  # another quarter
    # Its main sentence names no quarter ("the three months ended September 30, 2025"); its
    # subheadline does
    assert not announces_results(WERIDE_Q3, "FY2024Q2")
    notice = (
        "Klarna to Publish Q3 2026 Earnings on November 17, 2026\n"
        "NEW YORK--(BUSINESS WIRE)-- Klarna will report third quarter 2026 results, with revenue, "
        "on November 17."
    )
    assert not announces_results(notice, "FY2026Q3")
    news = (
        "WeRide and Uber Expand Robotaxi Partnership\n"
        "NEW YORK, Oct. 27, 2026 (GLOBE NEWSWIRE) -- WeRide today announced a partnership. "
        "Revenue from the service grew 40%."
    )
    assert not announces_results(news, "FY2026Q3")
    figures_only = KLARNA_Q4.replace("revenue", "volume").replace("Revenue", "Volume")
    assert not announces_results(figures_only, "FY2025Q4")
