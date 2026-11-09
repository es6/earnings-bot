import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from earnings_bot import date_discovery
from earnings_bot.browser_sources import IRTrial
from earnings_bot.calendar import Calendar, describe, manual_target
from earnings_bot.date_discovery import (
    CALENDAR_DATE,
    FMP_EARNINGS,
    DateDiscovery,
    find_date,
    parse_notice,
)
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.ir_research import IR_LISTINGS
from earnings_bot.issuers import profile
from earnings_bot.known import known_companies, to_target
from earnings_bot.recorder import ET
from tests.fakes import RoutedFetcher
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS

NOW = datetime(2026, 11, 7, 10, tzinfo=ET)
NVIDIA_BODY = (
    "<p>On Tuesday, November 17, 2026, at 2 p.m. PT, NVIDIA will host a conference call "
    "for the third quarter of fiscal year 2027.</p><p>This material will be posted to "
    "investor.nvidia.com immediately after the company's results are publicly announced "
    "at approximately 1:20 p.m. PT.</p>"
)
NVIDIA_FEED = "investor.nvidia.com/feed/PressRelease.svc/GetPressReleaseList"
NVIDIA_LINK = "/news/news-details/2026/NVIDIA-Sets-Conference-Call/default.aspx"


def nvidia_notices() -> tuple[int, bytes]:
    notice: dict[str, Any] = {
        "PressReleaseId": 1801,
        "Headline": "NVIDIA Sets Conference Call for Third-Quarter Financial Results",
        "LinkToDetailPage": NVIDIA_LINK,
        "Body": NVIDIA_BODY,
    }
    return 200, json.dumps({"GetPressReleaseListResult": [notice]}).encode()


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps desktop Chrome out of these tests: no IR listing read, no browser trial verified."""

    async def nothing(*args: object) -> None:
        return None

    monkeypatch.setattr(date_discovery, "_listing_date", nothing)
    monkeypatch.setattr(date_discovery, "verified_browser_trial", nothing)


def test_notice_prefers_report_time_over_call_and_preserves_fiscal_year() -> None:
    result = parse_notice(
        "NVDA",
        known_companies()["NVDA"],
        "NVIDIA Sets Conference Call for Third-Quarter Financial Results",
        NVIDIA_BODY,
        NOW,
    )
    assert result is not None
    target, evidence = result
    assert target.expected == datetime(2026, 11, 17, 16, 20, tzinfo=ET)
    assert target.period == "FY2027Q3"
    assert evidence == "Issuer confirmed release time"


def test_notice_arms_wider_window_after_close_and_leaves_call_only_pending() -> None:
    company = known_companies()["CRWD"]
    title = "CrowdStrike Announces Date of Fiscal Third Quarter 2027 Financial Results"
    result = parse_notice(
        "CRWD",
        company,
        title,
        "CrowdStrike will release its financial results on December 1, 2026 after market close. "
        "The conference call will start at 5:00 p.m. ET.",
        NOW,
    )
    assert result is not None
    target, evidence = result
    assert target.expected.date().isoformat() == "2026-12-01"
    assert target.expected.hour == 16
    assert target.before_minutes == 60
    assert "after market close" in evidence
    assert (
        parse_notice(
            "CRWD",
            company,
            title,
            "CrowdStrike will host a conference call on December 1, 2026 at 5 p.m. ET.",
            NOW,
        )
        is None
    )


AMD_FEED = "https://ir.amd.com/news-events/press-releases/rss"
AMD_NOTICE = "https://ir.amd.com/news-events/press-releases/detail/1300/amd-to-report-q4"
OFF_SITE = "https://elsewhere.example/amd-q4"


async def test_an_rss_notice_sets_the_date_and_off_site_links_arent_followed() -> None:
    title = "AMD to Report Fourth Quarter 2026 Financial Results"
    feed = f"""<rss version="2.0"><channel>
    <item><title>{title}</title><link>{OFF_SITE}</link><guid>a</guid></item>
    <item><title>{title}</title><link>{AMD_NOTICE}</link><guid>b</guid></item>
    </channel></rss>"""
    page = (
        "<p>AMD will report its fourth quarter 2026 financial results after the market close "
        "on February 2, 2027. AMD will hold a conference call at 5:00 p.m. ET.</p>"
    )
    fetcher = RoutedFetcher(
        {"press-releases/rss": (200, feed.encode()), "detail/1300": (200, page.encode())}
    )
    assert AMD_FEED in known_companies()["AMD"].rss

    result = await find_date(fetcher, "AMD", NOW)

    assert result is not None
    target, evidence = result
    # No time given, so AMD's usual time with a wider window
    assert target.expected == datetime(2027, 2, 2, 16, 15, tzinfo=ET)
    assert target.period == "FY2026Q4"
    assert (target.before_minutes, target.after_minutes) == (60, 90)
    assert evidence == f"Issuer date confirmed, after market close <{AMD_NOTICE}>"
    assert OFF_SITE not in [url for _, url, _ in fetcher.requests]


async def test_discovery_waits_out_an_open_window_then_saves_the_issuers_date(
    tmp_path: Path,
) -> None:
    fetcher = RoutedFetcher({NVIDIA_FEED: nvidia_notices()})
    window_open = True
    asked = asyncio.Event()

    def busy() -> bool:
        asked.set()
        return window_open

    statuses: list[str] = []
    saved = asyncio.Event()

    def status(message: str) -> None:
        statuses.append(message)
        saved.set()

    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA", 1, NOW)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, busy, status)
        task = asyncio.create_task(dates.run())
        try:
            await asyncio.wait_for(asked.wait(), 5)
            # Nothing is fetched while a release window is open
            assert fetcher.requests == []

            window_open = False
            dates.changed.set()
            await asyncio.wait_for(saved.wait(), 5)
            [row] = calendar.entries()
            assert row.target is not None
            assert row.target.expected == datetime(2026, 11, 17, 16, 20, tzinfo=ET)
            evidence = f"Issuer confirmed release time <https://investor.nvidia.com{NVIDIA_LINK}>"
            assert row.evidence == evidence
            assert statuses == [describe(row.target, evidence)]

            # Each issuer is checked about once an hour, so another /track doesn't recheck it
            fetched = len(fetcher.requests)
            asked.clear()
            dates.changed.set()
            await asyncio.wait_for(asked.wait(), 5)
            assert len(fetcher.requests) == fetched
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class InterruptedFetcher(RoutedFetcher):
    """Calls `interrupt` as each request goes out, to change things while discovery looks."""

    def __init__(self, routes: dict[str, tuple[int, bytes]], interrupt: Callable[[], None]) -> None:
        super().__init__(routes)
        self.interrupt = interrupt

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
    ) -> Response:
        self.interrupt()
        return await super().request(
            method, url, headers, give_up_after, urgent, allow_redirects=allow_redirects
        )


@pytest.mark.parametrize("interruption", ["window opens", "ticker removed"])
async def test_a_date_found_as_a_window_opens_or_after_untrack_isnt_saved(
    tmp_path: Path, interruption: str
) -> None:
    window_open = False
    statuses: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        row, _ = calendar.add("NVDA", 1, NOW)

        def interrupt() -> None:
            nonlocal window_open
            if interruption == "window opens":
                window_open = True
            else:
                calendar.remove(row.cik)

        fetcher = InterruptedFetcher({NVIDIA_FEED: nvidia_notices()}, interrupt)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: window_open, statuses.append)

        await dates._check(row)

        assert fetcher.requests
        assert all(entry.target is None for entry in calendar.entries())
    assert statuses == []


async def test_discovery_leaves_an_admin_override_alone(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({NVIDIA_FEED: nvidia_notices()})
    statuses: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA", 1, NOW)
        calendar.set_target(
            manual_target("NVDA", "2026-11-17T16:25", "FY2027Q3", NOW), "Manual override"
        )
        [row] = calendar.entries()
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, statuses.append)

        await dates._check(row)

        assert calendar.entries() == [row]
    assert fetcher.requests == []
    assert statuses == []


async def test_a_verified_browser_trial_joins_the_found_date_and_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = IRTrial(listing=IR_LISTINGS["NVDA"], headline="Third Quarter", verify_text=True)
    verified: list[str] = []

    async def verify(fetcher: Fetcher, ticker: str, period: str) -> IRTrial:
        verified.append(period)
        return trial

    monkeypatch.setattr(date_discovery, "verified_browser_trial", verify)
    fetcher = RoutedFetcher({NVIDIA_FEED: nvidia_notices()})
    statuses: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("NVDA", 1, NOW)
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, statuses.append)
        [row] = calendar.entries()

        await dates._check(row)

        [row] = calendar.entries()
        assert row.target and row.target.trial.ir == trial
        # The next hourly check finds the same date, and keeps the trial without verifying again
        await dates._check(row)
        assert calendar.entries() == [row]
    assert verified == ["FY2027Q3"]
    assert len(statuses) == 1


# Already investigated, so these tests see date discovery alone
LUCID = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, NOW.date()).model_copy(
    update={"investigated": NOW.date()}
)
FMP_ROUTE = "financialmodelingprep.com/stable/earnings?symbol=LCID"


def fmp_dates(*rows: tuple[str, float | None]) -> tuple[int, bytes]:
    return 200, json.dumps(
        [{"symbol": "LCID", "date": day, "epsActual": actual} for day, actual in rows]
    ).encode()


class Lucid:
    """Lucid tracked from SEC's data, with discovery that may ask FMP."""

    def __init__(self, store: DeliveryStore, fetcher: RoutedFetcher, key: str | None) -> None:
        self.calendar = Calendar(store)
        self.calendar.add("LCID", 1, NOW, LUCID)
        self.now = NOW
        self.statuses: list[str] = []
        self.dates = DateDiscovery(
            self.calendar, fetcher, lambda: self.now, lambda: False, self.statuses.append, key
        )

    async def check(self) -> None:
        [row] = self.calendar.entries()
        await self.dates._check(row)

    def row(self) -> tuple[datetime | None, str | None]:
        [row] = self.calendar.entries()
        return (row.target.expected if row.target else None), row.evidence


async def test_fmps_date_at_the_8k_time_sets_a_wide_window(tmp_path: Path) -> None:
    fetcher = RoutedFetcher(
        # Today's has been reported already, before the open
        {FMP_ROUTE: fmp_dates(("2027-02-24", None), ("2026-11-09", None), ("2026-11-07", -3.3))}
    )
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        lucid = Lucid(store, fetcher, "secret")

        await lucid.check()

        [row] = lucid.calendar.entries()
        assert row.target is not None
        # The soonest date still to report, at 4:05 PM from Lucid's 8-Ks, for the quarter that
        # ended on September 30
        assert row.target.expected == datetime(2026, 11, 9, 16, 5, tzinfo=ET)
        assert row.target.period == "FY2026Q3"
        assert (row.target.before_minutes, row.target.after_minutes) == (60, 90)
        assert row.target.prnewswire == LUCID.names
        assert row.evidence == CALENDAR_DATE
        assert lucid.statuses == [describe(row.target, CALENDAR_DATE)]
    # The key goes in a header, not the URL
    [(_, url, headers)] = fetcher.requests
    assert url == f"{FMP_EARNINGS}?symbol=LCID&limit=5"
    assert headers == {"apikey": "secret"}


async def test_fmp_is_asked_twice_a_day_until_its_window_passes(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({FMP_ROUTE: fmp_dates(("2026-11-09", None))})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        lucid = Lucid(store, fetcher, "secret")
        await lucid.check()
        await lucid.check()
        assert len(fetcher.requests) == 1

        # After the window closes, the remembered answer is no use
        fetcher.routes[FMP_ROUTE] = fmp_dates(("2026-11-09", -2.1), ("2027-02-24", None))
        lucid.now = datetime(2026, 11, 9, 17, 36, tzinfo=ET)
        await lucid.check()

        assert len(fetcher.requests) == 2
        assert lucid.row() == (datetime(2027, 2, 24, 16, 5, tzinfo=ET), CALENDAR_DATE)


@pytest.mark.parametrize("answer", [(500, b"busy"), (200, b"<html>"), (200, b'{"error": 1}')])
async def test_a_failed_fmp_answer_saves_nothing_and_is_asked_again(
    tmp_path: Path, answer: tuple[int, bytes]
) -> None:
    fetcher = RoutedFetcher({FMP_ROUTE: answer})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        lucid = Lucid(store, fetcher, "secret")

        await lucid.check()
        assert lucid.row() == (None, None)

        fetcher.routes[FMP_ROUTE] = fmp_dates(("2026-11-09", None))
        await lucid.check()
        assert lucid.row() == (datetime(2026, 11, 9, 16, 5, tzinfo=ET), CALENDAR_DATE)


async def test_a_symbol_fmps_plan_doesnt_cover_waits_for_the_next_refresh(
    tmp_path: Path,
) -> None:
    fetcher = RoutedFetcher({FMP_ROUTE: (402, b"Premium Query Parameter")})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        lucid = Lucid(store, fetcher, "secret")

        await lucid.check()
        await lucid.check()

        assert lucid.row() == (None, None)
    assert len(fetcher.requests) == 1


async def test_an_issuers_date_beats_fmp_and_fmp_needs_a_key(tmp_path: Path) -> None:
    fetcher = RoutedFetcher({FMP_ROUTE: fmp_dates(("2026-11-10", None))})
    confirmed = "Issuer confirmed release time <https://ir.lucidmotors.com/notice>"
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        lucid = Lucid(store, fetcher, "secret")
        target = to_target("LCID", datetime(2026, 11, 9, 16, 5, tzinfo=ET), 2026, 3, LUCID)
        lucid.calendar.set_target(target, confirmed)

        await lucid.check()

        assert lucid.row() == (target.expected, confirmed)
        assert fetcher.requests == []

    with DeliveryStore(tmp_path / "keyless.db", 7) as store:
        lucid = Lucid(store, fetcher, None)
        await lucid.check()
        assert lucid.row() == (None, None)
    assert fetcher.requests == []


@pytest.mark.parametrize(
    "facts", [{"release_time": None}, {"fiscal_year_end": None}], ids=["no-time", "no-year-end"]
)
async def test_fmp_isnt_asked_without_a_time_or_fiscal_year_to_go_with_its_date(
    tmp_path: Path, facts: dict[str, None]
) -> None:
    fetcher = RoutedFetcher({FMP_ROUTE: fmp_dates(("2026-11-09", None))})
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", 1, NOW, LUCID.model_copy(update=facts))
        [row] = calendar.entries()
        dates = DateDiscovery(calendar, fetcher, lambda: NOW, lambda: False, print, "secret")

        await dates._check(row)

        assert calendar.entries() == [row]
    assert fetcher.requests == []


def test_catalog_companies_have_no_fiscal_year_end_so_fmp_leaves_them_alone() -> None:
    # Their dates come from the issuer, which an FMP guess mustn't replace
    assert all(c.fiscal_year_end is None for c in known_companies().values())
