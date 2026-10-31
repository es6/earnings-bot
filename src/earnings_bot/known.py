"""Companies we've already researched, so adding one can start from what we know instead of
working it all out again. The facts live in companies.toml next to this file.

    uv run python -m earnings_bot.known AMD 2026-11-03T16:15 --fiscal-year 2026 --quarter 3

prints a `[[target]]` block for a schedule file.
"""

import argparse
import json
import sys
import tomllib
from datetime import date, datetime
from functools import cache
from pathlib import Path
from string import Formatter
from typing import Literal

from pydantic import BaseModel

from earnings_bot.recorder import ET, Target

COMPANIES = Path(__file__).with_name("companies.toml")
NTH = ("first", "second", "third", "fourth")
ORD = ("1st", "2nd", "3rd", "4th")
# The placeholders a guess template may use; companies.toml explains each one
FIELDS = frozenset({"yyyy", "mm", "dd", "fy", "fyy", "fq", "nth", "ord"})


class KnownCompany(BaseModel):
    name: str
    names: list[str] = []  # how wire headlines name the company
    cik: int
    platform: Literal["q4", "equisolve", "notified", "investis", "own-site", "unknown"]
    q4_host: str | None = None
    reachable: bool  # whether the IR site answers automated requests
    wire: Literal["Business Wire", "PR Newswire", "GlobeNewswire", "none", "unknown"]
    release_time: str | None = None  # HH:MM US Eastern
    eightk: list[str] = []  # last four earnings 8-Ks accepted, HH:MM US Eastern
    fastest: Literal["ir-feed", "company-rss", "company-page", "newswire", "sec-8k", "unknown"]
    rss: list[str] = []
    guess: list[str] = []  # URL templates
    evidence: str
    todo: str = ""
    checked: date


@cache
def known_companies(path: Path = COMPANIES) -> dict[str, KnownCompany]:
    with path.open("rb") as file:
        rows = tomllib.load(file)["company"]
    return {ticker: KnownCompany.model_validate(row) for ticker, row in rows.items()}


def template_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


def guess_urls(
    company: KnownCompany, released: date, fiscal_year: int, fiscal_quarter: int
) -> list[str]:
    """The company's guess templates filled in for one release. The fiscal year and quarter are
    the company's own labels, e.g. Walmart's Q2 FY27 is reported in August 2026."""
    values = {
        "yyyy": f"{released:%Y}",
        "mm": f"{released:%m}",
        "dd": f"{released:%d}",
        "fy": str(fiscal_year),
        "fyy": f"{fiscal_year % 100:02d}",
        "fq": str(fiscal_quarter),
        "nth": NTH[fiscal_quarter - 1],
        "ord": ORD[fiscal_quarter - 1],
    }
    return [template.format(**values) for template in company.guess]


def to_target(ticker: str, expected: datetime, fiscal_year: int, fiscal_quarter: int) -> Target:
    """A schedule entry for one release, built from what we know about the company."""
    company = known_companies()[ticker]
    return Target(
        ticker=ticker,
        expected=expected,
        cik=company.cik,
        q4_host=company.q4_host if company.reachable else None,
        # Business Wire's feed is the only newswire the bot reads so far
        newswire=company.names if company.wire in ("Business Wire", "unknown") else [],
        rss=company.rss,
        guess=guess_urls(company, expected.date(), fiscal_year, fiscal_quarter),
    )


def target_toml(target: Target) -> str:
    lines = ["[[target]]", f'ticker = "{target.ticker}"']
    local = target.expected.astimezone(ET).replace(tzinfo=None)
    lines.append(f"expected = {local.isoformat(timespec='seconds')}")
    lines.append(f"cik = {target.cik}")
    if target.q4_host:
        lines.append(f'q4_host = "{target.q4_host}"')
    for key in ("newswire", "rss", "guess"):
        values: list[str] = getattr(target, key)
        if values:
            lines += [f"{key} = [", *(f"    {json.dumps(v)}," for v in values), "]"]
    return "\n".join(lines)


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
