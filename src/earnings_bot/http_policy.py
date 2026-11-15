"""Server-directed waiting shared by polling, research and record-only trials."""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after(raw: str, now: datetime | None = None, default: float = 30) -> float:
    """Retry-After is either nonnegative seconds or an HTTP date, never local time."""
    raw = raw.strip()
    if re.fullmatch(r"[0-9]+", raw):
        return float(raw)
    try:
        stamp = parsedate_to_datetime(raw)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return max(0, (stamp - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return default
