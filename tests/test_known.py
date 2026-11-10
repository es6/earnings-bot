import tomllib
from datetime import date, datetime
from pathlib import Path

import pytest

from earnings_bot.known import (
    FIELDS,
    fill,
    fiscal_period,
    guess_urls,
    known_companies,
    naming_offset,
    planned_period,
    target_toml,
    template_fields,
    to_target,
)
from earnings_bot.recorder import ET, Plan, load_plan

SCHEDULES = Path(__file__).parent.parent / "schedules"


def test_every_company_loads_and_uses_only_known_placeholders() -> None:
    companies = known_companies()
    assert len(companies) >= 40
    for ticker, company in companies.items():
        assert ticker.isupper()
        for template in [*company.guess, *company.trial.pages]:
            assert template_fields(template) <= FIELDS, (ticker, template)
        if company.platform == "q4" and company.reachable:
            assert company.q4_host, ticker


def guesses_in(schedule: str, ticker: str) -> list[str]:
    return next(t.guess for t in load_plan(SCHEDULES / schedule).target if t.ticker == ticker)


@pytest.mark.parametrize(
    ("ticker", "expected", "fiscal_year", "quarter", "schedule"),
    [
        ("UBER", datetime(2026, 11, 4, 6, 55), 2026, 3, "2026-11-04.toml"),
        ("AAPL", datetime(2026, 11, 2, 16, 30), 2026, 4, "2026-11-02.toml"),
    ],
)
def test_templates_rebuild_the_hand_written_schedules(
    ticker: str, expected: datetime, fiscal_year: int, quarter: int, schedule: str
) -> None:
    target = to_target(ticker, expected, fiscal_year, quarter)
    assert target.guess == guesses_in(schedule, ticker)


@pytest.mark.parametrize(
    ("ticker", "released", "fiscal_year", "quarter", "seen"),
    [
        # Each URL was found on the company's own site on 2026-10-31
        ("GS", date(2026, 10, 13), 2026, 3,
         "https://www.goldmansachs.com/pressroom/press-releases/2026/2026-10-13-q3-results"),
        ("GS", date(2026, 1, 15), 2025, 4,
         "https://www.goldmansachs.com/pressroom/press-releases/2026/2026-01-15-q4-results"),
        ("UNH", date(2026, 7, 16), 2026, 2,
         "https://www.unitedhealthgroup.com/newsroom/2026/2026-07-16-uhg-reports-second-quarter-2026-results.html"),
        ("TGT", date(2026, 5, 20), 2026, 1,
         "https://corporate.target.com/press/release/2026/05/target-corporation-reports-first-quarter-earnings"),
        ("WMT", date(2026, 8, 20), 2027, 2,
         "https://corporate.walmart.com/news/2026/08/20/walmart-releases-q2-fy27-earnings"),
        ("MSFT", date(2026, 10, 28), 2027, 1,
         "https://www.microsoft.com/en-us/investor/earnings/fy-2027-q1/press-release-webcast"),
        ("QCOM", date(2026, 7, 29), 2026, 3,
         "https://s204.q4cdn.com/645488518/files/doc_financials/2026/q3/FY2026-3rd-Quarter-Earnings-Release.pdf"),
        # Each URL answered 200 on 2026-11-05
        ("ABNB", date(2026, 8, 6), 2026, 2,
         "https://s26.q4cdn.com/656283129/files/doc_financials/2026/q2/Airbnb-Q2-2026-Shareholder-Letter.pdf"),
        ("ABNB", date(2026, 5, 7), 2026, 1,
         "https://s26.q4cdn.com/656283129/files/doc_financials/2026/q1/Airbnb_Q1-2026-Shareholder-Letter.pdf"),
        ("TEAM", date(2026, 8, 6), 2026, 4,
         "https://s206.q4cdn.com/270053503/files/doc_financials/2026/q4/TEAM-Q4-2026-Shareholder-Letter.pdf"),
        ("TEAM", date(2026, 8, 6), 2026, 4,
         "https://s206.q4cdn.com/270053503/files/doc_financials/2026/q4/TEAM-Q4-2026-Datasheet.pdf"),
        ("TEAM", date(2026, 4, 30), 2026, 3,
         "https://s206.q4cdn.com/270053503/files/doc_financials/2026/q3/TEAM-Q3-2026-IR-Data-Sheet.pdf"),
        ("NET", date(2026, 8, 6), 2026, 2,
         "https://www.cloudflare.com/press/press-releases/2026/cloudflare-announces-second-quarter-2026-financial-results/"),
        ("ABNB", date(2026, 11, 5), 2026, 3,
         "https://s26.q4cdn.com/656283129/files/content_files/Airbnb-Q3-2026-Shareholder-Letter.pdf"),
        ("TEAM", date(2026, 11, 5), 2027, 1,
         "https://s206.q4cdn.com/270053503/files/content_files/TEAM-Q1-2027-Shareholder-Letter.pdf"),
        ("TEAM", date(2026, 11, 5), 2027, 1,
         "https://s206.q4cdn.com/270053503/files/doc_financials/2027/q1/TEAM-Q1-2027-Earnings-Release.pdf"),
    ],
)  # fmt: skip
def test_templates_match_urls_seen_on_company_sites(
    ticker: str, released: date, fiscal_year: int, quarter: int, seen: str
) -> None:
    assert seen in guess_urls(known_companies()[ticker], released, fiscal_year, quarter)


@pytest.mark.parametrize(
    ("year_end", "released", "period"),
    [
        ("1231", date(2026, 11, 9), (2026, 3)),  # Lucid
        ("1231", date(2027, 2, 24), (2026, 4)),
        ("0125", date(2026, 11, 17), (2027, 3)),  # NVIDIA's FY2027 ends in January 2027
        ("0131", date(2026, 11, 19), (2027, 3)),  # Walmart
        ("0926", date(2026, 10, 29), (2026, 4)),  # Apple
        ("0831", date(2026, 12, 10), (2027, 1)),  # Costco
        ("0630", date(2027, 1, 28), (2027, 2)),  # Microsoft
        # A release within a week of a quarter's end reports the quarter before it
        ("1231", date(2026, 10, 7), (2026, 3)),
        ("1231", date(2026, 10, 6), (2026, 2)),
        ("0229", date(2026, 12, 20), (2027, 3)),  # a year end that only leap years have
    ],
)
def test_a_release_date_gives_the_fiscal_period_it_reports(
    year_end: str, released: date, period: tuple[int, int]
) -> None:
    assert fiscal_period(year_end, released) == period


def test_soft_404_sites_get_no_guesses() -> None:
    # UnitedHealth answers 200 for pages that don't exist, so a guess would fire at once
    unh = to_target("UNH", datetime(2027, 1, 15, 5, 55), 2026, 4)
    assert unh.guess == []
    assert unh.newswire == ["UnitedHealth Group"]
    assert to_target("GS", datetime(2027, 1, 14, 7, 30), 2026, 4).guess


def test_blocked_sites_and_other_wires_are_left_out_of_the_target() -> None:
    fico = to_target("FICO", datetime(2026, 11, 4, 16, 15), 2026, 4)
    assert fico.q4_host is None
    assert fico.newswire == ["FICO", "Fair Isaac"]
    lilly = to_target("LLY", datetime(2027, 2, 4, 6, 45), 2026, 4)
    assert (lilly.newswire, lilly.prnewswire) == ([], ["Lilly"])
    assert [s.name for s in lilly.sources()] == [
        "edgar",
        "edgar_latest",
        "prnewswire_busted",
        "prnewswire",
    ]


@pytest.mark.parametrize(
    ("day", "ticker", "fiscal_year", "quarter"),
    [
        ("2026-11-04", "HUBS", 2026, 3),
        ("2026-11-04", "FICO", 2026, 4),
        ("2026-11-05", "ABNB", 2026, 3),
        ("2026-11-05", "TEAM", 2027, 1),
        ("2026-11-05", "NET", 2026, 3),
    ],
)
def test_schedule_entries_match_the_database(
    day: str, ticker: str, fiscal_year: int, quarter: int
) -> None:
    plan = load_plan(SCHEDULES / f"{day}.toml")
    [scheduled] = [t for t in plan.target if t.ticker == ticker]
    expected = scheduled.expected.replace(tzinfo=None)
    database = to_target(ticker, expected, fiscal_year, quarter)
    # A run can teach us new guesses, trial sources and spelled feeds, which past schedules
    # don't have
    assert set(scheduled.guess) <= set(database.guess)
    update = {
        "guess": scheduled.guess,
        "trial": scheduled.trial,
        "period": scheduled.period,
        "rss_spelled": scheduled.rss_spelled,
    }
    assert database.model_copy(update=update) == scheduled


@pytest.mark.parametrize("ticker", ["AMD", "MCD", "AVGO", "CRWD", "ABNB", "HUBS", "NVDA"])
def test_printed_entry_is_a_valid_schedule(ticker: str) -> None:
    target = to_target(ticker, datetime(2026, 11, 3, 16, 15), 2026, 3)
    plan = Plan.model_validate(tomllib.loads(target_toml(target)))
    assert plan.target[0] == target
    assert plan.target[0].expected == datetime(2026, 11, 3, 16, 15, tzinfo=ET)


@pytest.mark.parametrize(
    ("ticker", "released", "fiscal_year", "quarter", "seen"),
    [
        # Each answered 200 on 2026-11-06
        ("CRWD", date(2026, 8, 26), 2027, 2,
         "https://markets.financialcontent.com/stocks/article/bizwire-2026-8-26-crowdstrike-reports-second-quarter-fiscal-year-2027-financial-results"),
        ("MCD", date(2026, 11, 4), 2026, 3,
         "https://mcdonalds.mediaroom.com/2026-11-04-McDONALDS-REPORTS-THIRD-QUARTER-2026-RESULTS"),
        # Redirects to the page, which ends in a slash
        ("ABNB", date(2026, 11, 5), 2026, 3,
         "https://news.airbnb.com/airbnb-q3-2026-financial-results"),
        # A fourth quarter's headline adds the full year
        ("CRWD", date(2027, 3, 3), 2027, 4,
         "https://markets.financialcontent.com/stocks/article/bizwire-2027-3-3-crowdstrike-reports-fourth-quarter-and-fiscal-year-2027-financial-results"),
        ("MCD", date(2027, 2, 10), 2026, 4,
         "https://mcdonalds.mediaroom.com/2027-02-10-McDONALDS-REPORTS-FOURTH-QUARTER-AND-FULL-YEAR-2026-RESULTS"),
    ],
)  # fmt: skip
def test_trial_page_templates_match_the_release_pages(
    ticker: str, released: date, fiscal_year: int, quarter: int, seen: str
) -> None:
    pages = known_companies()[ticker].trial.pages
    assert fill(pages, released, fiscal_year, quarter) == [seen]


def test_broadcom_is_matched_on_pr_newswire_and_tried_on_its_newsroom() -> None:
    avgo = to_target("AVGO", datetime(2026, 12, 10, 16, 15), 2026, 4)

    assert avgo.prnewswire == ["Broadcom Inc"]
    assert [s.name for s in avgo.sources()] == [
        "edgar",
        "edgar_latest",
        "prnewswire_busted",
        "prnewswire",
        "broadcom_newsroom",
        "prnewswire_semiconductors",
    ]


@pytest.mark.parametrize(
    ("reported", "offset"),
    [
        # Home Depot's "Second Quarter Fiscal 2026" and "First Quarter Fiscal 2026"
        ([(date(2026, 8, 18), (2026, 2)), (date(2026, 5, 19), (2026, 1))], -1),
        # NVIDIA's "Second Quarter Fiscal 2027"
        ([(date(2026, 8, 26), (2027, 2))], 0),
        # Walmart's Q4 release looks ahead to "Q1 and FY27", another quarter
        ([(date(2026, 2, 19), (2027, 1))], None),
        # Two years off is a misreading, not a way of naming years
        ([(date(2026, 5, 20), (2025, 1))], None),
        # Split evenly, so neither can be trusted
        ([(date(2026, 8, 18), (2026, 2)), (date(2026, 8, 26), (2027, 2))], None),
        # The majority wins over one misreading
        (
            [
                (date(2026, 8, 18), (2026, 2)),
                (date(2026, 5, 19), (2027, 1)),
                (date(2026, 2, 24), (2025, 4)),
            ],
            -1,
        ),
        ([], None),
    ],
)
def test_how_a_company_names_its_fiscal_years_comes_from_its_results(
    reported: list[tuple[date, tuple[int, int]]], offset: int | None
) -> None:
    assert naming_offset("0131", reported) == offset


@pytest.mark.parametrize(
    ("year_end", "offset", "released", "period"),
    [
        ("0131", -1, date(2026, 11, 17), (2026, 3)),  # Home Depot's Q3 fiscal 2026
        ("0131", -1, date(2026, 11, 18), (2026, 3)),  # Lowe's
        ("0201", -1, date(2026, 11, 19), (2026, 3)),  # Target
        ("0131", 0, date(2026, 11, 18), (2027, 3)),  # NVIDIA's Q3 fiscal 2027
        ("0131", 0, date(2026, 12, 2), (2027, 3)),  # Salesforce
        ("0131", None, date(2026, 11, 19), (2027, 3)),  # Walmart, whose releases never say
    ],
)
def test_a_planned_period_is_named_as_the_company_names_it(
    year_end: str, offset: int | None, released: date, period: tuple[int, int]
) -> None:
    company = known_companies()["NVDA"].model_copy(
        update={"fiscal_year_end": year_end, "fiscal_year_offset": offset}
    )
    assert planned_period(company, released) == period
    assert planned_period(company.model_copy(update={"fiscal_year_end": None}), released) is None
