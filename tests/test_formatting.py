from datetime import datetime

import pytest

from earnings_bot.browser_sources import BrowserRelease
from earnings_bot.formatting import (
    bullets,
    clock,
    duration,
    period_label,
    source_label,
    watch_list,
)
from earnings_bot.recorder import ET
from earnings_bot.sources import PR_NEWSWIRE_EARNINGS, EdgarFilings, EdgarLatest, RssFeed


def test_a_fiscal_period_reads_like_a_quarter() -> None:
    assert period_label("FY2026Q3") == "Q3 FY26"
    assert period_label("FY2027Q1") == "Q1 FY27"
    assert period_label(None) == ""


@pytest.mark.parametrize(
    ("hour", "minute", "text"),
    [(0, 5, "12:05 AM ET"), (9, 30, "9:30 AM ET"), (12, 0, "12:00 PM ET"), (16, 5, "4:05 PM ET")],
)
def test_times_are_eastern_on_a_12_hour_clock(hour: int, minute: int, text: str) -> None:
    assert clock(datetime(2026, 11, 9, hour, minute, tzinfo=ET)) == text


def test_alert_times_keep_their_milliseconds() -> None:
    moment = datetime(2026, 11, 9, 16, 6, 2, 708_400, tzinfo=ET)
    assert clock(moment, seconds=True) == "4:06:02 PM ET"
    assert clock(moment, millis=True) == "4:06:02.708 PM ET"
    # Another zone's time is shown in Eastern
    assert clock(datetime.fromisoformat("2026-11-09T21:06:02+00:00")) == "4:06 PM ET"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.234, "0.23s"),
        (9.994, "9.99s"),
        (10, "10s"),
        (59.4, "59s"),
        (60, "1m"),
        (171.31, "2m 51s"),
        (3600, "1h"),
        (3780, "1h 3m"),
    ],
)
def test_durations_read_at_a_glance(seconds: float, text: str) -> None:
    assert duration(seconds) == text


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("edgar", "SEC 8-K filings"),
        ("edgar_latest", "SEC 8-K filings"),
        ("SEC 6-K acceptance", "SEC 6-K filings"),
        ("businesswire_busted", "Business Wire earnings feed"),
        ("prnewswire_busted", "PR Newswire earnings feed"),
        ("prnewswire_semiconductors", "PR Newswire semiconductor feed"),
        ("globenewswire", "GlobeNewswire earnings feed"),
        ("browser-ir", "IR press release page, read in Chrome"),
        ("browser-pdf:q3.pdf", "IR file q3.pdf, read in Chrome"),
        ("q4_press", "IR site press releases and files (Q4 feed)"),
        ("q4_files", "IR site press releases and files (Q4 feed)"),
        ("rss:ir.example.com", "ir.example.com press release feed"),
        ("guess:Q3-26-Results.pdf", "Guessed file Q3-26-Results.pdf"),
        ("page:ir.example.com", "Guessed page on ir.example.com"),
        ("broadcom_newsroom", "Broadcom newsroom"),
        ("something_new", "something_new"),
    ],
)
def test_each_source_has_a_plain_name(name: str, label: str) -> None:
    form = "6-K" if "6-K" in name else "8-K"
    assert source_label(name, form) == label


def test_copies_of_a_source_share_a_line_and_trials_are_marked() -> None:
    cached = RssFeed("prnewswire", PR_NEWSWIRE_EARNINGS, ["Lucid Group"])
    fresh = RssFeed("prnewswire_busted", PR_NEWSWIRE_EARNINGS, ["Lucid Group"])
    globe = RssFeed("globenewswire", "https://www.globenewswire.com/rss", ["Lucid Group"])
    globe_busted = RssFeed("globenewswire_busted", "https://www.globenewswire.com/rss", ["Lucid"])
    globe.trial = globe_busted.trial = True
    chrome = BrowserRelease("https://ir.lucidmotors.com/press-releases", "Results")
    # A line is only on trial if every copy is
    fresh.trial = True
    sec = [EdgarFilings(1811210, form="6-K"), EdgarLatest(1811210, form="6-K")]

    assert watch_list([*sec, fresh, cached, chrome, globe, globe_busted]) == [
        "SEC 6-K filings",
        "PR Newswire earnings feed",
        "IR press release page, read in Chrome",
        "GlobeNewswire earnings feed *(trial: recorded, not posted)*",
    ]


def test_a_long_list_says_how_much_didnt_fit() -> None:
    assert bullets(["one", "two"]) == "• one\n• two"
    assert bullets([]) == ""
    # Four of these fit in 200 characters only without the note about the rest
    lines = [f"line {i:02d} " + "x" * 38 for i in range(40)]
    text = bullets(lines, limit=200)
    assert len(text) <= 200
    shown = text.splitlines()[:-1]
    assert shown == [f"• {line}" for line in lines[: len(shown)]]
    assert text.splitlines()[-1] == f"-# …and {len(lines) - len(shown)} more"
