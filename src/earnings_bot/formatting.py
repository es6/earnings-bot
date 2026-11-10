"""How the bot words things in Discord: source names, times, durations and fiscal periods."""

from collections.abc import Iterable
from datetime import datetime

from earnings_bot.browser_sources import HttpRelease, QuarterlyTable
from earnings_bot.recorder import ET
from earnings_bot.sources import EdgarFilings, Source

# Embed colors
BLUE = 0x3B82F6  # working
GREEN = 0x22C55E  # ready
AMBER = 0xF59E0B  # waiting on something
RED = 0xEF4444  # failed
GREY = 0x6B7280  # removed or idle


def period_label(period: str | None) -> str:
    """FY2026Q3 as Q3 FY26."""
    return f"Q{period[-1]} FY{period[4:6]}" if period else ""


def clock(moment: datetime, seconds: bool = False, millis: bool = False) -> str:
    """4:05 PM ET, 4:05:02 PM ET or 4:05:02.708 PM ET."""
    local = moment.astimezone(ET)
    hour = local.hour % 12 or 12
    text = f"{hour}:{local:%M}"
    if seconds or millis:
        text += f":{local:%S}"
    if millis:
        text += f".{local.microsecond // 1000:03d}"
    return f"{text} {local:%p} ET"


def day(moment: datetime) -> str:
    """Thu, Nov 12."""
    local = moment.astimezone(ET)
    return f"{local:%a, %b} {local.day}"


def stamp(moment: datetime, style: str = "f") -> str:
    """A Discord timestamp, shown in each reader's own time zone."""
    return f"<t:{int(moment.timestamp())}:{style}>"


def duration(seconds: float) -> str:
    """0.23s, 57s, 2m 51s or 1h 3m."""
    if seconds < 10:
        return f"{seconds:.2f}s"
    whole = round(seconds)
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def source_label(name: str, form: str = "8-K") -> str:
    """What a source is, by its name in schedules and the journal."""
    if name in ("edgar", "edgar_latest") or name.startswith("SEC "):
        return f"SEC {form} filings"
    if name.startswith("businesswire"):
        return "Business Wire earnings feed"
    if name == "prnewswire_semiconductors":
        return "PR Newswire semiconductor feed"
    if name.startswith("prnewswire"):
        return "PR Newswire earnings feed"
    if name.startswith("globenewswire"):
        return "GlobeNewswire earnings feed"
    if name == "browser-ir":
        return "IR press release page, read in Chrome"
    if name == "http-ir":
        return "IR press release page, direct HTTP"
    if name == "issuer-news":
        return "IR newsroom API and results PDF, direct HTTP"
    if name == "issuer-table":
        return "IR quarterly results PDF"
    if name == "browser-rss":
        return "IR press release feed, read in Chrome"
    if name.startswith(("browser-pdf:", "tesla-pdf:")):
        return f"IR file {name.split(':', 1)[1]}, read in Chrome"
    if name in ("q4_press", "q4_files"):
        return "IR site press releases and files (Q4 feed)"
    if name == "broadcom_newsroom":
        return "Broadcom newsroom"
    if name.startswith("rss:"):
        return f"{name.removeprefix('rss:')} press release feed"
    if name.startswith("guess:"):
        return f"Guessed file {name.removeprefix('guess:')}"
    if name.startswith("page:"):
        return f"Guessed page on {name.removeprefix('page:')}"
    return name


def watch_list(sources: Iterable[Source]) -> list[str]:
    """One line per kind of source, in the order given. Copies of one feed share a line."""
    lines: dict[str, None] = {}
    for source in sources:
        form = source.form if isinstance(source, EdgarFilings) else "8-K"
        label = source_label(source.name, form)
        if isinstance(source, QuarterlyTable):
            label += ", read in Chrome" if not source.direct else ", direct HTTP"
        elif isinstance(source, HttpRelease) and source.chrome_fallback:
            label += " (Chrome fallback)"
        if source.spellings and not source.name.endswith("_busted"):
            # PR Newswire's spelled copy already says it's cache-busted
            label += ", new URL spelling each poll"
        lines[label] = None
    return list(lines)


def bullets(lines: Iterable[str], limit: int = 1024, bullet: str = "• ") -> str:
    """A bulleted list that fits one embed field, saying how many lines didn't fit."""
    lines = list(lines)
    text = ""
    for i, line in enumerate(lines):
        rest = len(lines) - i - 1
        candidate = f"{text}\n{bullet}{line}" if text else f"{bullet}{line}"
        more = f"\n-# …and {rest} more" if rest else ""
        if len(candidate) + len(more) > limit:
            return text + f"\n-# …and {len(lines) - i} more"
        text = candidate
    return text


def truncate(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
