"""Companies we've already researched, so adding one can start from what we know instead of
working it all out again. The facts live in companies.toml next to this file.

    uv run python -m earnings_bot.known AMD 2026-11-03T16:15 --fiscal-year 2026 --quarter 3

prints a `[[target]]` block for a schedule file.
"""

import argparse
import json
import sys
import tomllib
from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path
from string import Formatter
from typing import Literal

from pydantic import BaseModel

from earnings_bot.periods import NTH, ORD
from earnings_bot.recorder import ET, Target, Trial

COMPANIES = Path(__file__).with_name("companies.toml")
# The placeholders a URL template may use; companies.toml explains each one
FIELDS = frozenset({"yyyy", "mm", "dd", "m", "d", "fy", "fyy", "fq", "nth", "NTH", "ord", "q4"})


class SourceCheck(BaseModel):
    """One source the bot's investigation of a company looked at, and what it decided."""

    source: str  # what was checked, such as "Q4 feed on ir.example.com"
    watch: bool  # whether it's watched for the next release
    why: str


class KnownCompany(BaseModel):
    name: str
    names: list[str] = []  # how wire headlines name the company
    cik: int
    platform: Literal["q4", "equisolve", "notified", "investis", "own-site", "unknown"]
    q4_host: str | None = None
    reachable: bool  # whether the IR site answers automated requests
    soft_404: bool = False  # the site answers 200 for pages that don't exist
    wire: Literal["Business Wire", "PR Newswire", "GlobeNewswire", "none", "unknown"]
    release_time: str | None = None  # HH:MM US Eastern
    # The release time is when the IR site or a wire had the last release, not SEC's filing
    release_published: bool = False
    eightk: list[str] = []  # last four earnings 8-Ks accepted, HH:MM US Eastern
    sixk: list[str] = []  # verified results 6-Ks accepted, HH:MM US Eastern
    sec_form: Literal["8-K", "6-K"] | None = None
    fastest: Literal["ir-feed", "company-rss", "company-page", "newswire", "sec-8k", "unknown"]
    rss: list[str] = []
    guess: list[str] = []  # URL templates
    trial: Trial = Trial()  # sources on trial; `pages` holds URL templates here
    evidence: str
    todo: str = ""
    checked: date
    # SEC's MMDD, kept for companies added from SEC data so a calendar date gets its quarter
    fiscal_year_end: str | None = None
    # The IR site's press release listing and the direct request's status (0 for no response).
    ir_listing: str | None = None
    ir_http_status: int | None = None
    ir_news_api: str | None = None
    # The IR site's home page, where reading its navigation starts
    ir_home: str | None = None
    # The public data behind its results page, which can name new results before its newsroom
    ir_results_api: str | None = None
    ir_results_page: str | None = None
    ir_browser_rss: str | None = None
    # The latest results on it, and when the IR site says they were posted
    ir_release: str | None = None
    ir_posted: datetime | None = None
    # When the bot last looked for this company's sources itself, for one added from SEC data
    investigated: date | None = None
    # What that investigation checked, kept and rejected, and which version of it ran
    checks: list[SourceCheck] = []
    investigation: int = 0


@cache
def known_companies(path: Path = COMPANIES) -> dict[str, KnownCompany]:
    with path.open("rb") as file:
        rows = tomllib.load(file)["company"]
    return {ticker: KnownCompany.model_validate(row) for ticker, row in rows.items()}


def template_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


@dataclass(frozen=True)
class _InQ4:
    """`{q4:TEXT}` in a template is TEXT for a fourth-quarter release and nothing otherwise, for
    headlines that add "and Full Year" once a year."""

    fourth: bool

    def __format__(self, text: str) -> str:
        return text if self.fourth else ""


def fill(
    templates: Sequence[str], released: date, fiscal_year: int, fiscal_quarter: int
) -> list[str]:
    """URL templates filled in for one release. The fiscal year and quarter are the company's
    own labels, e.g. Walmart's Q2 FY27 is reported in August 2026."""
    values = {
        "yyyy": f"{released:%Y}",
        "mm": f"{released:%m}",
        "dd": f"{released:%d}",
        "m": str(released.month),
        "d": str(released.day),
        "fy": str(fiscal_year),
        "fyy": f"{fiscal_year % 100:02d}",
        "fq": str(fiscal_quarter),
        "nth": NTH[fiscal_quarter - 1],
        "NTH": NTH[fiscal_quarter - 1].upper(),
        "ord": ORD[fiscal_quarter - 1],
        "q4": _InQ4(fiscal_quarter == 4),
    }
    return [template.format(**values) for template in templates]


def guess_urls(
    company: KnownCompany, released: date, fiscal_year: int, fiscal_quarter: int
) -> list[str]:
    """The company's guess templates filled in for one release."""
    return fill(company.guess, released, fiscal_year, fiscal_quarter)


def wires(company: KnownCompany) -> tuple[list[str], list[str]]:
    """Names to match on Business Wire and on PR Newswire. A company whose wire isn't known is
    looked for on both."""
    unknown = company.wire == "unknown"
    return (
        company.names if unknown or company.wire == "Business Wire" else [],
        company.names if unknown or company.wire == "PR Newswire" else [],
    )


def fiscal_period(year_end: str, released: date) -> tuple[int, int]:
    """The fiscal year and quarter most likely reported on `released`, for a fiscal year that
    ends on `year_end` (SEC's MMDD). Results come out at least a week after the quarter ends, and
    a fiscal year is named for the calendar year it ends in, as NVIDIA's FY2027 ends in January
    2027. Quarter ends are taken as three months apart, close enough for 52-week years."""
    month, day = int(year_end[:2]), int(year_end[2:])
    ends = []
    for fiscal_year in range(released.year - 1, released.year + 2):
        for quarter in range(1, 5):
            year, end_month = divmod(fiscal_year * 12 + month - 1 - 3 * (4 - quarter), 12)
            end_month += 1
            end = date(year, end_month, min(day, monthrange(year, end_month)[1]))
            ends.append((end, fiscal_year, quarter))
    _, fiscal_year, quarter = max(e for e in ends if e[0] <= released - timedelta(days=7))
    return fiscal_year, quarter


def to_target(
    ticker: str,
    expected: datetime,
    fiscal_year: int,
    fiscal_quarter: int,
    company: KnownCompany | None = None,
) -> Target:
    """A schedule entry for one release, built from what we know about the company: its
    catalog entry unless another is given."""
    company = company or known_companies()[ticker]
    business_wire, pr_newswire = wires(company)
    pages = fill(company.trial.pages, expected.date(), fiscal_year, fiscal_quarter)
    # A company whose wire isn't known is looked for on all three
    globe = company.trial.globenewswire or (company.names if company.wire == "unknown" else [])
    return Target(
        ticker=ticker,
        expected=expected,
        cik=company.cik,
        sec_form=company.sec_form or "8-K",
        before_minutes=30 if company.sec_form == "6-K" else 10,
        period=f"FY{fiscal_year}Q{fiscal_quarter}",
        q4_host=company.q4_host if company.reachable else None,
        newswire=business_wire,
        prnewswire=pr_newswire,
        rss=company.rss,
        # A guess counts as found on a 200, which a soft 404 would fake from the first poll
        guess=[]
        if company.soft_404
        else guess_urls(company, expected.date(), fiscal_year, fiscal_quarter),
        trial=company.trial.model_copy(update={"pages": pages, "globenewswire": globe}),
    )


def target_toml(target: Target) -> str:
    lines = ["[[target]]", f'ticker = "{target.ticker}"']
    local = target.expected.astimezone(ET).replace(tzinfo=None)
    lines.append(f"expected = {local.isoformat(timespec='seconds')}")
    lines.append(f"cik = {target.cik}")
    if target.period:
        lines.append(f'period = "{target.period}"')
    if target.sec_form != "8-K":
        lines += [f'sec_form = "{target.sec_form}"', f"before_minutes = {target.before_minutes:g}"]
    if target.q4_host:
        lines.append(f'q4_host = "{target.q4_host}"')
    for key in ("newswire", "prnewswire", "rss", "guess"):
        lines += _toml_list(key, getattr(target, key))
    if target.ir:
        lines += ["", "[target.ir]"]
        lines += [
            f"{key} = {json.dumps(value)}"
            for key, value in target.ir.model_dump(exclude_defaults=True).items()
        ]
    trial = target.trial
    if trial != Trial(post=trial.post):
        lines += ["", "# Additional live sources", "[target.trial]"]
        if trial.broadcom_newsroom:
            lines.append("broadcom_newsroom = true")
        for key in ("rss", "globenewswire", "prnewswire_semiconductors", "pages"):
            lines += _toml_list(key, getattr(trial, key))
    return "\n".join(lines)


def _toml_list(key: str, values: list[str]) -> list[str]:
    return [f"{key} = [", *(f"    {json.dumps(v)}," for v in values), "]"] if values else []


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="earnings_bot.known", description="Print a schedule entry for a known company."
    )
    parser.add_argument("ticker", type=str.upper)
    parser.add_argument("expected", type=datetime.fromisoformat, help="release time, US Eastern")
    parser.add_argument("--fiscal-year", type=int, required=True)
    parser.add_argument("--quarter", type=int, choices=range(1, 5), required=True)
    args = parser.parse_args()
    company = known_companies().get(args.ticker)
    if company is None:
        sys.exit(f"{args.ticker} isn't in {COMPANIES.name} yet")
    target = to_target(args.ticker, args.expected, args.fiscal_year, args.quarter)
    print(f"# {company.name}: {company.evidence}")
    if company.todo:
        print(f"# Still to check: {company.todo}")
    print(target_toml(target))


if __name__ == "__main__":
    main()
