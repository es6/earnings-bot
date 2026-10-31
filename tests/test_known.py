import tomllib
from datetime import date, datetime
from pathlib import Path

import pytest

from earnings_bot.known import (
    FIELDS,
    guess_urls,
    known_companies,
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
        for template in company.guess:
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
    ],
)  # fmt: skip
def test_templates_match_urls_seen_on_company_sites(
    ticker: str, released: date, fiscal_year: int, quarter: int, seen: str
) -> None:
    assert seen in guess_urls(known_companies()[ticker], released, fiscal_year, quarter)


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
    assert [s.name for s in lilly.sources()] == ["edgar", "prnewswire"]


@pytest.mark.parametrize("ticker", ["AMD", "MCD"])
def test_printed_entry_is_a_valid_schedule(ticker: str) -> None:
    target = to_target(ticker, datetime(2026, 11, 3, 16, 15), 2026, 3)
    plan = Plan.model_validate(tomllib.loads(target_toml(target)))
    assert plan.target[0] == target
    assert plan.target[0].expected == datetime(2026, 11, 3, 16, 15, tzinfo=ET)
