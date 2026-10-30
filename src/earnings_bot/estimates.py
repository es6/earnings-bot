"""Consensus estimates for a report, from Financial Modeling Prep (FMP)."""

import json
from dataclasses import dataclass
from datetime import date, timedelta

from earnings_bot.fetch import Fetcher

FMP_CALENDAR = "https://financialmodelingprep.com/stable/earnings-calendar"


@dataclass(frozen=True)
class Estimates:
    eps: float | None
    revenue: float | None  # dollars

    def describe(self) -> str:
        """For the summary prompt."""
        lines = []
        if self.eps is not None:
            lines.append(f"EPS (the company's headline measure, usually adjusted): ${self.eps:.2f}")
        if self.revenue is not None:
            lines.append(f"Revenue: {format_dollars(self.revenue)}")
        return "\n".join(lines) or "None available"


def format_dollars(amount: float) -> str:
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(amount) >= scale:
            return f"${amount / scale:.2f}{suffix}"
    return f"${amount:,.0f}"


class EstimatesError(Exception):
    pass


async def fmp_estimates(fetcher: Fetcher, api_key: str, ticker: str, day: date) -> Estimates:
    # Calendars disagree by a day now and then, so look one day either side
    start, end = day - timedelta(days=1), day + timedelta(days=1)
    # The key goes in a header rather than the URL, which can end up in logs
    response = await fetcher.request(
        "GET", f"{FMP_CALENDAR}?from={start}&to={end}", headers={"apikey": api_key}
    )
    if response.status != 200:
        raise EstimatesError(f"FMP answered HTTP {response.status}: {response.body[:200]!r}")
    rows = json.loads(response.body)
    if not isinstance(rows, list):
        raise EstimatesError(f"FMP sent something other than a list: {str(rows)[:200]}")
    matches = [row for row in rows if row.get("symbol") == ticker]
    if not matches:
        raise EstimatesError(f"FMP's calendar has no {ticker} report between {start} and {end}")
    row = min(matches, key=lambda row: abs(date.fromisoformat(row["date"]) - day))
    return Estimates(eps=row.get("epsEstimated"), revenue=row.get("revenueEstimated"))
