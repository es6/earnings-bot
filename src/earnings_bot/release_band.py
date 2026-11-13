"""When a company usually releases its results, so a window polls the issuer's own routes at
full speed only from just before then.

Nebius's newsroom refused us on Nov 12 after an hour of one request a second from one address.
Most companies release at the same time every quarter: across 50 US filers, 88% of their last
eight results 8-Ks were accepted within 10 minutes of that company's usual time (measured Nov 12
from SEC acceptance times). Before the usual time, issuer routes are polled every SLOW_INTERVAL;
from LEAD before it, at their own speed. SEC filings and newswires are polled as before.

SEC's acceptance time is a proxy: it usually trails the press release by a minute or a few. The
expected time, which comes from an issuer's notice or our own timing of the last release, wins
when it is earlier.
"""

import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from earnings_bot.fetch import Fetcher
from earnings_bot.sources import EdgarFilings, EdgarLatest, Source

# Defined here as well as in recorder, which imports this module
ET = ZoneInfo("America/New_York")
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

SLOW_INTERVAL = 15.0  # seconds between issuer-route polls before the usual time
LEAD = timedelta(minutes=1)  # full speed from this long before the earliest usual time
QUARTERS = 8  # the most recent results filings considered
KEEP = 6 / 8  # the usual time covers six of the last eight releases
FEWEST = 4  # with fewer results filings in the session, there is no usual time
WIDEST = 30  # minutes; a wider spread means no usual time
WIRES = ("businesswire", "prnewswire", "globenewswire")


def issuer_route(source: Source) -> bool:
    """A route on the company's own site, as opposed to SEC or a newswire's shared feed."""
    return not isinstance(source, EdgarFilings | EdgarLatest) and not source.name.startswith(WIRES)


def results_minutes(submissions: dict, afternoon: bool) -> list[int]:
    """Minutes after midnight ET at which SEC accepted the latest results 8-Ks (Item 2.02),
    newest first, from the same half of the day as the coming release."""
    recent = submissions["filings"]["recent"]
    minutes = []
    for form, items, accepted in zip(
        recent["form"], recent.get("items", []), recent["acceptanceDateTime"], strict=False
    ):
        if form != "8-K" or "2.02" not in (items or ""):
            continue
        local = datetime.fromisoformat(accepted).astimezone(ET)
        if (local.hour >= 12) == afternoon:
            minutes.append(local.hour * 60 + local.minute)
        if len(minutes) == QUARTERS:
            break
    return minutes


def usual_start(minutes: list[int]) -> int | None:
    """The earliest minute of the tightest span holding six of the last eight releases, or None
    without enough of them close together."""
    if len(minutes) < FEWEST:
        return None
    keep = math.ceil(len(minutes) * KEEP)
    ordered = sorted(minutes)
    start, width = min(
        ((ordered[i], ordered[i + keep - 1] - ordered[i]) for i in range(len(ordered) - keep + 1)),
        key=lambda span: span[1],
    )
    return start if width <= WIDEST else None


async def full_speed_from(
    fetcher: Fetcher, cik: int, sec_form: str, expected: datetime
) -> datetime | None:
    """When the issuer's routes go to full speed for a release expected at `expected`. None
    keeps them at full speed for the whole window: a 6-K filer (its 6-K comes hours after the
    release), a spread-out history, or no answer from SEC."""
    if sec_form != "8-K":
        return None
    response = await fetcher.request("GET", SEC_SUBMISSIONS_URL.format(cik=cik), give_up_after=5)
    if response.status != 200:
        return None
    local = expected.astimezone(ET)
    try:
        start = usual_start(results_minutes(json.loads(response.body), local.hour >= 12))
    except (ValueError, KeyError, TypeError):
        return None
    if start is None:
        return None
    usual = local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    return min(usual, expected) - LEAD
