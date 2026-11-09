"""Discover a calendar window from a public issuer's earnings notice."""

import asyncio
import json
import logging
import os
import re
import time
from calendar import month_name
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from earnings_bot.browser import BrowserError
from earnings_bot.calendar import Calendar, Tracked, describe, same_release
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.investigate import OpenChrome, due, investigate
from earnings_bot.ir_research import issuer_links, issuer_listing, verified_browser_trial
from earnings_bot.known import (
    NTH,
    ORD,
    KnownCompany,
    fiscal_period,
    known_companies,
    to_target,
)
from earnings_bot.recorder import ET, PollError, Target, poll
from earnings_bot.sec_results import SixKResults, SixKUnavailable
from earnings_bot.sources import BroadcomNewsroom, Q4PressReleases, RssFeed
from earnings_bot.text import html_to_text, release_text

log = logging.getLogger(__name__)
PT = ZoneInfo("America/Los_Angeles")
FMP_EARNINGS = "https://financialmodelingprep.com/stable/earnings"
CALENDAR_DATE = "FMP calendar date, time from past 8-Ks"
CALENDAR_6K_DATE = "FMP calendar date, time estimated from verified results 6-Ks"
CALENDAR_REFRESH = 12 * 3600  # seconds; FMP's free plan allows 250 requests a day
MANUAL = "Manual override"
_MONTHS = {name.lower(): i for i, name in enumerate(month_name) if name}
_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    rf"\b(?:Q([1-4])|({'|'.join(NTH + ORD)})[-\s]+quarter)"
    r"\s+(?:of\s+)?(?:fiscal\s+)?(?:year\s+)?(?:FY\s*)?(20\d{2})\b",
    re.IGNORECASE,
)
_NOTICE = re.compile(
    r"sets?|schedule|announces? (?:the )?date|will (?:report|release|announce)|"
    r"to (?:report|release|announce|host)|conference call|webcast",
    re.IGNORECASE,
)
_RESULTS = re.compile(r"\b(?:earnings|results)\b", re.IGNORECASE)
_RELEASE = re.compile(
    r"\b(?:will|plans to|expects to)\s+(?:report|release|announce|publish|issue)\b"
    r"|\b(?:results|earnings)\b.{0,50}\b(?:will be|are)\s+"
    r"(?:publicly\s+)?(?:announced|released|published)\b",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"\bat\s+(?:approximately\s+|about\s+)?(\d{1,2})(?::(\d{2}))?\s*"
    r"(am|pm)\s*\(?(ET|EST|EDT|Eastern(?: Time)?|PT|PST|PDT|Pacific(?: Time)?)\b",
    re.IGNORECASE,
)
_AFTER = re.compile(
    r"after (?:the )?(?:(?:US|U S) )?(?:market close|market closes|closing bell)", re.IGNORECASE
)
_BEFORE = re.compile(
    r"before (?:the )?(?:(?:US|U S) )?(?:market open|market opens|opening bell)", re.IGNORECASE
)
_CALL = re.compile(r"\b(?:conference call|webcast)\b", re.IGNORECASE)


def _is_notice(title: str) -> bool:
    return bool(_NOTICE.search(title) and _RESULTS.search(title))


def _dates(text: str, today: date) -> set[date]:
    result = set()
    for month, day, year in _DATE.findall(text):
        for y in (int(year),) if year else (today.year, today.year + 1):
            try:
                candidate = date(y, _MONTHS[month.lower()], int(day))
            except ValueError:
                continue
            if today <= candidate <= today + timedelta(days=120):
                result.add(candidate)
    return result


def _release_time(text: str, day: date) -> datetime | None:
    # Keep the host's call time out even when both times share a sentence.
    for match in _TIME.finditer(text):
        if _CALL.search(text[: match.start()]):
            continue
        hour, minute, half, zone = match.groups()
        if not 1 <= int(hour) <= 12 or not 0 <= int(minute or 0) <= 59:
            continue
        tz = PT if zone.casefold().startswith(("p",)) else ET
        return datetime(
            day.year,
            day.month,
            day.day,
            int(hour) % 12 + (12 if half.lower() == "pm" else 0),
            int(minute or 0),
            tzinfo=tz,
        ).astimezone(ET)
    return None


def parse_notice(
    ticker: str, company: KnownCompany, title: str, body: str, now: datetime
) -> tuple[Target, str] | None:
    if not _is_notice(title):
        return None
    text = " ".join(html_to_text(body).split())
    text = re.sub(r"\b([ap])\.m\.", r"\1m", text, flags=re.IGNORECASE).replace("U.S.", "US")
    period = _PERIOD.search(f"{title} {text}")
    if period is None:
        return None
    digit, ordinal, year = period.groups()
    quarter = (
        int(digit)
        if digit
        else (
            NTH.index(ordinal.lower()) + 1
            if ordinal.lower() in NTH
            else ORD.index(ordinal.lower()) + 1
        )
    )
    today = now.astimezone(ET).date()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    release = [sentence for sentence in sentences if _RELEASE.search(sentence)]
    if not release:
        return None
    dates = set().union(*(_dates(sentence, today) for sentence in release))
    if not dates:
        dates = _dates(f"{title} {text}", today)
    if len(dates) != 1:
        return None
    [day] = dates
    expected = next((stamp for s in release if (stamp := _release_time(s, day))), None)
    if expected is not None:
        if expected <= now:
            return None
        target = to_target(ticker, expected, int(year), quarter, company)
        return target, "Issuer confirmed release time"

    # The issuer confirmed the date. Use a wider watch for an approximate time of day.
    if any(_AFTER.search(sentence) for sentence in release):
        fallback = "16:15"
        mode = "after market close"
    elif any(_BEFORE.search(sentence) for sentence in release):
        fallback = "07:00"
        mode = "before market open"
    else:
        fallback = company.release_time
        mode = "release time estimated"
    if not fallback:
        return None
    hour, minute = map(int, (company.release_time or fallback).split(":"))
    if (mode == "after market close" and hour < 16) or (mode == "before market open" and hour >= 9):
        hour, minute = map(int, fallback.split(":"))
    expected = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
    target = to_target(ticker, expected, int(year), quarter, company)
    target.before_minutes = 60
    target.after_minutes = 90
    return (target, f"Issuer date confirmed, {mode}") if target.window[1] > now else None


async def find_date(
    fetcher: Fetcher, ticker: str, now: datetime, company: KnownCompany | None = None
) -> tuple[Target, str] | None:
    """The next release from the issuer's own notice, for a catalog company unless another is
    given."""
    company = company or known_companies()[ticker]
    if company.q4_host and company.reachable:
        source = Q4PressReleases(company.q4_host)
        url = source.bodies_url(20)
        try:
            reply = await fetcher.request("GET", url, give_up_after=4, allow_redirects=False)
            if reply.status == 200:
                for row in json.loads(reply.body)["GetPressReleaseListResult"]:
                    if result := parse_notice(
                        ticker, company, row["Headline"], row.get("Body") or "", now
                    ):
                        target, evidence = result
                        link = urljoin(
                            f"https://{company.q4_host}", row.get("LinkToDetailPage") or ""
                        )
                        return target, f"{evidence} <{link}>"
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            pass
    for url in dict.fromkeys([*company.rss, *company.trial.rss]):
        try:
            feed = RssFeed("calendar", url, bust_cache=True)
            reply = await fetcher.request("GET", feed.url(), give_up_after=4, allow_redirects=False)
            if reply.status != 200:
                continue
            notices = [item for item in feed.parse(reply) if _is_notice(item.title)]
            for item in notices[:3]:
                link = urlsplit(item.url)
                if link.scheme != "https" or link.netloc != urlsplit(url).netloc:
                    continue
                page = await fetcher.request(
                    "GET", item.url, give_up_after=4, allow_redirects=False
                )
                if page.status == 200:
                    body = page.body.decode(errors="replace")
                    if result := parse_notice(ticker, company, item.title, body, now):
                        target, evidence = result
                        return target, f"{evidence} <{item.url}>"
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            continue
    if company.trial.broadcom_newsroom:
        source = BroadcomNewsroom()
        try:
            _, releases = await poll(source, fetcher)
            for item in [item for item in releases if _is_notice(item.title)][:3]:
                body = await release_text(fetcher, source, replace(item, earnings=True))
                if body and (result := parse_notice(ticker, company, item.title, body, now)):
                    target, evidence = result
                    return target, f"{evidence} <{item.url}>"
        except PollError:
            pass
    return await _listing_date(fetcher, ticker, company, now)


async def calendar_date(
    fetcher: Fetcher, api_key: str, ticker: str, company: KnownCompany, now: datetime
) -> tuple[Target, str] | None:
    """The next release from FMP's earnings calendar, for a company with no issuer feed we
    know. The day is FMP's, and the time comes from prior results filings. None
    means FMP has no date to come; ValueError means it didn't answer properly."""
    if not (company.release_time and company.fiscal_year_end):
        return None
    # The key goes in a header rather than the URL, which can end up in logs
    reply = await fetcher.request(
        "GET", f"{FMP_EARNINGS}?symbol={ticker}&limit=5", {"apikey": api_key}, give_up_after=4
    )
    if reply.status == 402:  # the plan doesn't cover this symbol, such as BRK-B on the free one
        return None
    if reply.status != 200:
        raise ValueError(f"FMP answered HTTP {reply.status}")
    rows = json.loads(reply.body)
    if not isinstance(rows, list):
        raise ValueError("FMP sent something other than a list")
    today = now.astimezone(ET).date()
    days = sorted(
        day
        for row in rows
        if row.get("symbol") == ticker
        and row.get("epsActual") is None
        and (day := date.fromisoformat(row["date"])) >= today
    )
    hour, minute = map(int, company.release_time.split(":"))
    for day in days:
        expected = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
        fiscal_year, quarter = fiscal_period(company.fiscal_year_end, day)
        target = to_target(ticker, expected, fiscal_year, quarter, company)
        target.before_minutes = 60
        target.after_minutes = 90
        if target.window[1] > now:
            return target, CALENDAR_6K_DATE if company.sec_form == "6-K" else CALENDAR_DATE
    return None


async def _listing_date(
    fetcher: Fetcher, ticker: str, company: KnownCompany, now: datetime
) -> tuple[Target, str] | None:
    listing = issuer_listing(ticker, company)
    if listing is None:
        return None

    async def direct(url: str) -> Response:
        return await fetcher.request("GET", url, give_up_after=4, allow_redirects=False)

    async def check(get: Callable[[str], Awaitable[Response]]) -> tuple[Target, str] | None:
        page = await get(listing)
        if page.status != 200:
            return None
        notices = [
            (url, title) for url, title in issuer_links(listing, page.body) if _is_notice(title)
        ]
        for url, title in notices[:3]:
            page = await get(url)
            if page.status == 200:
                body = page.body.decode(errors="replace")
                if result := parse_notice(ticker, company, title, body, now):
                    target, evidence = result
                    return target, f"{evidence} <{url}>"
        return None

    with suppress(aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        if result := await check(direct):
            return result
    if os.name != "nt":
        return None
    try:
        async with fetcher.research_browser() as browser:
            return await check(lambda url: browser.request(url, listing, 4, fetcher.wait_turn))
    except (BrowserError, aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        return None
    return None


class DateDiscovery:
    def __init__(
        self,
        calendar: Calendar,
        fetcher: Fetcher,
        now: Callable[[], datetime],
        busy: Callable[[], bool],
        status: Callable[[str], None],
        fmp_api_key: str | None = None,
        chrome: OpenChrome | None = None,
        sixk: SixKResults | None = None,
    ) -> None:
        self.calendar, self.fetcher, self.now = calendar, fetcher, now
        self.busy, self.status = busy, status
        self.fmp_api_key = fmp_api_key
        self.chrome = chrome  # for investigations; None means desktop Chrome on Windows
        self.sixk = sixk or SixKResults(fetcher)
        self.phases: dict[int, str] = {}
        self.changed = asyncio.Event()
        self.checked: dict[int, float] = {}
        # Each company's FMP answer and when to ask again, as time.monotonic()
        self.calendar_dates: dict[int, tuple[float, tuple[Target, str] | None]] = {}

    async def run(self) -> None:
        while True:
            self.changed.clear()
            for row in self.calendar.entries():
                if self.busy():
                    break
                if time.monotonic() < self.checked.get(row.cik, 0):
                    continue
                self.checked[row.cik] = time.monotonic() + 3600
                try:
                    # Room for a first investigation, which may wait for Chrome
                    async with asyncio.timeout(150):
                        await self._check(row)
                except Exception:
                    log.exception("Date discovery failed for %s", row.ticker)
                    self.phases[row.cik] = "Issuer research failed. Retrying at the next check."
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=60)

    async def _calendar_date(self, row: Tracked, now: datetime) -> tuple[Target, str] | None:
        if self.fmp_api_key is None:
            return None
        # A remembered answer counts until it's due again, unless its window has passed
        asked = self.calendar_dates.get(row.cik)
        if asked and time.monotonic() < asked[0] and (not asked[1] or asked[1][0].window[1] > now):
            return asked[1]
        try:
            result = await calendar_date(
                self.fetcher, self.fmp_api_key, row.ticker, row.company, now
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            return None  # asked again at the next check
        self.calendar_dates[row.cik] = time.monotonic() + CALENDAR_REFRESH, result
        return result

    async def _issuer_date(self, row: Tracked, now: datetime) -> tuple[Target, str] | None:
        if result := await find_date(self.fetcher, row.ticker, now, row.company):
            return result
        if row.company.sec_form != "6-K":
            return None
        with suppress(SixKUnavailable):
            async for notice in self.sixk.notices(row.cik, now):
                if result := parse_notice(
                    row.ticker, row.company, notice.headline, escape(notice.text), now
                ):
                    target, evidence = result
                    return target, f"{evidence} from issuer 6-K exhibit <{notice.url}>"
        return None

    async def _investigate(self, row: Tracked) -> Tracked:
        """Looks for the sources of a company outside the catalog, once and then monthly."""
        today = self.now().astimezone(ET).date()
        if not due(row.ticker, row.company, today):
            return row
        self.phases[row.cik] = "Looking for its IR site, feeds and newswire."
        chrome = self.chrome or (self.fetcher.research_browser if os.name == "nt" else None)
        company = await investigate(
            self.fetcher, row.company, today, chrome, sixk=self.sixk, ticker=row.ticker
        )
        if self.busy() or row not in self.calendar.entries():
            return row
        self.calendar.save_profile(row.cik, company)
        self.status(f"{row.ticker}: {company.evidence}")
        return replace(row, company=company)

    async def _check(self, row: Tracked) -> None:
        row = await self._investigate(row)
        if self._defer_if_busy(row.cik):
            return
        self.phases[row.cik] = "Checking issuer notices for the next release date."
        now = self.now()
        saved = (
            (row.target, row.evidence)
            if row.target and row.evidence and row.target.window[1] > now
            else None
        )
        # An issuer's word beats FMP's calendar, which beats nothing
        if saved and saved[1] == MANUAL:
            target, evidence = saved
        elif result := await self._issuer_date(row, now):
            target, evidence = result
        elif saved and saved[1] not in (CALENDAR_DATE, CALENDAR_6K_DATE):
            target, evidence = saved
        elif result := await self._calendar_date(row, now):
            target, evidence = result
        elif saved:
            target, evidence = saved
        else:
            self.phases[row.cik] = (
                "No confirmed date found. A Manage Server member can set one with "
                "`/track` using `when` and `period`. Rechecking hourly."
            )
            return
        if self._defer_if_busy(row.cik) or row not in self.calendar.entries():
            return
        if row.target and row.target.trial.ir and same_release(target, row.target):
            target.trial.ir = row.target.trial.ir
        if row.company.sec_form == "6-K" and target.sec_form != "6-K":
            target = target.model_copy(update={"sec_form": "6-K"})
        self.phases.pop(row.cik, None)
        if row.target != target or row.evidence != evidence:
            self.calendar.set_target(target, evidence)
            self.status(describe(target, evidence))
        if target.trial.ir or target.trial.post or not target.period:
            return
        trial = await verified_browser_trial(
            self.fetcher, issuer_listing(row.ticker, row.company), target.period
        )
        if (
            trial
            and not self.busy()
            and any(
                tracked.target == target and tracked.evidence == evidence
                for tracked in self.calendar.entries()
            )
        ):
            prepared = target.trial.model_copy(update={"ir": trial})
            self.calendar.set_target(target.model_copy(update={"trial": prepared}), evidence)

    def _defer_if_busy(self, cik: int) -> bool:
        if not self.busy():
            return False
        self.checked.pop(cik, None)
        self.phases[cik] = "Issuer research queued after release monitoring."
        return True
