"""How often a window polls each route: slowly far from the times a company releases at, and at
full speed only close to them.

Nebius's newsroom refused us on Nov 12 after an hour of one request a second from one address.
Releases come at the time we expect them, or a little later: of 149 past quarterly releases
placed by the company's own earlier ones, 146 came no sooner than a minute before that time, two
came three and four minutes early, and one switched to the other side of the trading day; 90%
came within five minutes after it and three more than 30 minutes after it (measured Nov 14,
scratchpad `early_margin.py`). So every route but SEC polls

- every SLOW seconds from the window's start,
- every NEAR seconds from RAMP before each release time,
- at its own speed from LEAD before it to HOLD after it,
- every NEAR seconds after the last release time's full-speed stretch, until the window closes.

SEC is the exception: its fair-access limit is ten requests a second, a window uses about one,
and its filings are the fallback when an issuer's site refuses us.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from earnings_bot.sources import EdgarFilings, EdgarLatest, Source

# Defined here as well as in recorder, which imports this module
ET = ZoneInfo("America/New_York")

SLOW = 15.0  # seconds between polls far from any release time
NEAR = 5.0  # seconds between polls just before a release time, and after the last one
RAMP = timedelta(minutes=5)  # NEAR from this long before a release time
LEAD = timedelta(minutes=2)  # full speed from this long before a release time
HOLD = timedelta(minutes=10)  # and until this long after it
# A window opens at least this long before the earliest release time and stays open at least
# this long after the latest
OPEN_BEFORE = timedelta(minutes=15)
OPEN_AFTER = timedelta(minutes=30)
RECENT = 4  # the most recent releases whose times a window covers
_OPEN, _CLOSE = (9, 30), (16, 0)  # the regular trading session, US Eastern


def paced(source: Source) -> bool:
    """Whether a route follows the release times: every route but SEC's."""
    return not isinstance(source, EdgarFilings | EdgarLatest)


def pace(at: datetime, times: Sequence[datetime], own: float) -> tuple[float, float | None]:
    """Seconds between polls at `at` for a route whose own speed is `own`, and seconds until
    that changes (None when it won't). Without release times, the route's own speed. A route
    never polls faster than its own speed."""
    if not times:
        return own, None
    every = NEAR if at >= max(times) + HOLD else SLOW
    edges: list[datetime] = []
    for moment in times:
        if moment - LEAD <= at < moment + HOLD:
            every = min(every, own)
        elif moment - RAMP <= at < moment - LEAD:
            every = min(every, NEAR)
        edges += [moment - RAMP, moment - LEAD, moment + HOLD]
    later = [edge for edge in edges if edge > at]
    change = (min(later) - at).total_seconds() if later else None
    return max(every, own), change


def _session(moment: datetime) -> int:
    clock = moment.astimezone(ET)
    hhmm = (clock.hour, clock.minute)
    return 0 if hhmm < _OPEN else (1 if hhmm < _CLOSE else 2)


def release_times(history: Sequence[datetime], expected: datetime) -> list[datetime]:
    """The times on `expected`'s day that the company's recent releases (newest first) came out
    at, US Eastern, in the same part of the trading day as `expected`, with `expected` itself."""
    day: date = expected.astimezone(ET).date()
    times = {expected}
    for moment in history[:RECENT]:
        clock = moment.astimezone(ET)
        placed = datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=ET)
        if _session(placed) == _session(expected):
            times.add(placed)
    return sorted(times)
