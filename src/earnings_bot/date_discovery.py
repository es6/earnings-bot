"""Discover a calendar window from a public issuer's earnings notice."""

import asyncio
import json
import logging
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
from earnings_bot.investigate import OpenChrome, Report, due, investigate
from earnings_bot.ir_research import (
    chrome_available,
    issuer_links,
    issuer_listing,
    verified_browser_source,
    verified_http_source,
)
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
from earnings_bot.sources import BroadcomNewsroom, Q4PressReleases, RssFeed, is_date_notice
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
_PERIOD_END = re.compile(r"\b(?:ended|ending)\s+(?:on\s+)?$", re.IGNORECASE)
# Abbreviated months, as in "Nov. 12, 2026", which would also end a sentence too soon
_ABBREVIATED = re.compile(
    r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?(?=\s+\d)", re.IGNORECASE
)
_NOTICE_QUARTER = rf"(?:Q([1-4])|({'|'.join(NTH + ORD)})[-\s]+quarter)"
_PERIOD = re.compile(
    rf"\b{_NOTICE_QUARTER}"
    # "of fiscal 2026", "and fiscal 2026", "and full year 2025"
    r"\s+(?:(?:and|&)\s+(?:full[-\s]+)?|of\s+)?(?:fiscal\s+)?(?:year\s+)?(?:FY\s*)?(20\d{2})\b",
    re.IGNORECASE,
)
_YEAR_FIRST_PERIOD = re.compile(
    rf"\b(?:fiscal(?:\s+year)?|FY)\s*(20\d{{2}})\s+{_NOTICE_QUARTER}\b",
    re.IGNORECASE,
)
VERIFYING_IR = "Verifying the issuer's last report"
_RELEASE = re.compile(
    r"\b(?:will|plans to|expects to|to)\s+(?:report|release|announce|publish|issue)\b"
    r"(?:(?!\b(?:conference call|earnings call|webcast)\b)[^.!?]){0,100}?"
    r"\b(?:results|earnings)\b"
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
    r"after (?:the )?(?:(?:US|U S) )?"
    r"(?:market closes?|closing bell|close of (?:the )?market)\b",
    re.IGNORECASE,
)
_BEFORE = re.compile(
    r"before (?:the )?(?:(?:US|U S) )?"
    r"(?:market opens?|opening bell|open(?:ing)? of (?:the )?market)\b",
    re.IGNORECASE,
)
_CALL = re.compile(r"\b(?:conference call|earnings call|webcast)\b", re.IGNORECASE)
_SAME_DAY = re.compile(r"\b(?:that|the same)\s+(?:day|morning|afternoon|evening)\b", re.IGNORECASE)
_BREAK = re.compile(r"<br\b[^>]*>", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|--+|[\u2013\u2014]")


def _full_months(text: str) -> str:
    return _ABBREVIATED.sub(
        lambda match: next(
            name for name in _MONTHS if name.startswith(match.group(1)[:3].lower())
        ).capitalize(),
        text,
    )


def _dates(text: str, today: date, *, release: bool = False) -> set[date]:
    result = set()
    for match in _DATE.finditer(text):
        if release and _PERIOD_END.search(text[: match.start()]):
            continue
        month, day, year = match.groups()
        for y in (int(year),) if year else (today.year, today.year + 1):
            try:
                candidate = date(y, _MONTHS[month.lower()], int(day))
            except ValueError:
                continue
            if today <= candidate <= today + timedelta(days=120):
                result.add(candidate)
    return result


def _release_time(text: str, day: date, *, allow_call: bool = False) -> datetime | None:
    # Keep the host's call time out even when both times share a sentence.
    for match in _TIME.finditer(text):
        if not allow_call and _CALL.search(text[: match.start()]):
            continue
        # A time right after another day's date, such as when the page was posted, isn't it
        dated = list(_DATE.finditer(text[max(0, match.start() - 30) : match.start()]))
        if dated and (_MONTHS[dated[-1].group(1).lower()], int(dated[-1].group(2))) != (
            day.month,
            day.day,
        ):
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


def _release_clause(statement: str) -> str | None:
    match = _RELEASE.search(statement)
    if match is None:
        return None
    before = _CALL.search(statement, 0, match.start())
    if before and statement[match.start() :].casefold().startswith("to "):
        return None  # A call to discuss or announce results is still a call.
    start = match.start() if before else 0
    after = _CALL.search(statement, match.end())
    return statement[start : after.start() if after else None]


def parse_notice(
    ticker: str, company: KnownCompany, title: str, body: str, now: datetime
) -> tuple[Target, str] | None:
    if not is_date_notice(title):
        return None

    def tidy(line: str) -> str:
        line = _full_months(" ".join(line.split()))
        return re.sub(r"\b([ap])\.m\.", r"\1m", line, flags=re.IGNORECASE).replace("U.S.", "US")

    title = tidy(title)
    # Kept apart, so a heading or the page's "posted at" line doesn't run into the next line
    lines = [tidy(line) for line in html_to_text(_BREAK.sub(" ", body)).splitlines()]
    # The headline's explicit fiscal period takes precedence over other quarters in the page.
    for notice in (title, " ".join(lines)):
        if period := _PERIOD.search(notice):
            digit, ordinal, year = period.groups()
            break
        if period := _YEAR_FIRST_PERIOD.search(notice):
            year, digit, ordinal = period.groups()
            break
    else:
        return None
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
    # A headline such as "X to Report Fourth Quarter Results on November 12, 2026" may be the
    # only place that gives the date, when the notice itself is about the call
    sentences = [title, *(s for line in lines for s in _SENTENCE.split(line))]
    release = [part for sentence in sentences if (part := _release_clause(sentence))]
    if not release:
        return None
    dates = set().union(*(_dates(sentence, today, release=True) for sentence in release))
    if (
        not dates
        and not any(_DATE.search(sentence) for sentence in release)
        and any(_SAME_DAY.search(sentence) or _TIME.search(sentence) for sentence in release)
    ):
        # Some notices date the call, then give a separate release time for that day.
        dates = set().union(
            *(
                _dates(sentence, today)
                for sentence in sentences
                if _CALL.search(sentence) and _release_clause(sentence) is None
            )
        )
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
    estimate = expected
    if mode == "before market open":
        # The issuer's same-day earnings call is a deadline for results, even when the
        # historical SEC acceptance times would put the estimate later than the call.
        for sentence in sentences:
            if (
                _CALL.search(sentence)
                and (_SAME_DAY.search(sentence) or day in _dates(sentence, today))
                and (call := _release_time(sentence, day, allow_call=True))
            ):
                expected = min(expected, call)
    target = to_target(ticker, expected, int(year), quarter, company)
    target.before_minutes = 90 if expected < estimate else 60
    target.after_minutes = 90
    return (target, f"Issuer date confirmed, {mode}") if target.window[1] > now else None


async def find_date(
    fetcher: Fetcher,
    ticker: str,
    now: datetime,
    company: KnownCompany | None = None,
    unread: list[str] | None = None,
) -> tuple[Target, str] | None:
    """The next release from the issuer's own notice, for a catalog company unless another is
    given. The headline of each notice whose date couldn't be read goes in `unread`."""
    company = company or known_companies()[ticker]
    unread = [] if unread is None else unread
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
                    if is_date_notice(row["Headline"]):
                        unread.append(row["Headline"])
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            pass
    for url in dict.fromkeys([*company.rss, *company.trial.rss]):
        try:
            feed = RssFeed("calendar", url, bust_cache=True)
            reply = await fetcher.request("GET", feed.url(), give_up_after=4, allow_redirects=False)
            if reply.status != 200:
                continue
            notices = [item for item in feed.parse(reply) if is_date_notice(item.title)]
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
                    unread.append(item.title)
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            continue
    if company.trial.broadcom_newsroom:
        source = BroadcomNewsroom()
        try:
            _, releases = await poll(source, fetcher)
            for item in [item for item in releases if is_date_notice(item.title)][:3]:
                body = await release_text(fetcher, source, replace(item, earnings=True))
                if body and (result := parse_notice(ticker, company, item.title, body, now)):
                    target, evidence = result
                    return target, f"{evidence} <{item.url}>"
                unread.append(item.title)
        except PollError:
            pass
    return await _listing_date(fetcher, ticker, company, now, unread)


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
    fetcher: Fetcher, ticker: str, company: KnownCompany, now: datetime, unread: list[str]
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
            (url, title) for url, title in issuer_links(listing, page.body) if is_date_notice(title)
        ]
        for url, title in notices[:3]:
            page = await get(url)
            if page.status == 200:
                body = page.body.decode(errors="replace")
                if result := parse_notice(ticker, company, title, body, now):
                    target, evidence = result
                    return target, f"{evidence} <{url}>"
                if title not in unread:
                    unread.append(title)
        return None

    with suppress(aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        if result := await check(direct):
            return result
    if not chrome_available():
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
        # Each running investigation, so the scorecard can follow it
        self.reports: dict[int, Report] = {}
        # Why each tracked company's IR page is or isn't watched in its next window
        self.ir_notes: dict[int, str] = {}
        # Called whenever a phase, report or note changes
        self.on_progress: Callable[[], None] = lambda: None
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
                await self.run_once(row)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=60)

    async def run_once(self, row: Tracked) -> None:
        """One hourly check of a company, which never raises."""
        try:
            # Room for a first investigation, which reads several pages through Chrome
            async with asyncio.timeout(300):
                await self._check(row)
        except Exception:
            log.exception("Date discovery failed for %s", row.ticker)
            self._phase(row.cik, "Research failed. Retrying in an hour.")
        finally:
            if self.reports.pop(row.cik, None):
                self.on_progress()

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

    async def _issuer_date(
        self, row: Tracked, now: datetime, unread: list[str]
    ) -> tuple[Target, str] | None:
        if result := await find_date(self.fetcher, row.ticker, now, row.company, unread):
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

    def _phase(self, cik: int, text: str | None) -> None:
        if text is None:
            self.phases.pop(cik, None)
        else:
            self.phases[cik] = text
        self.on_progress()

    async def _investigate(self, row: Tracked) -> Tracked:
        """Looks for the sources of a company outside the catalog, once and then monthly."""
        today = self.now().astimezone(ET).date()
        if not due(row.ticker, row.company, today):
            return row
        self._phase(row.cik, "Investigating its sources")
        report = self.reports[row.cik] = Report(changed=self.on_progress)
        chrome = self.chrome or (self.fetcher.research_browser if chrome_available() else None)
        company = await investigate(
            self.fetcher,
            row.company,
            today,
            chrome,
            sixk=self.sixk,
            ticker=row.ticker,
            report=report,
        )
        if self.busy() or row not in self.calendar.entries():
            return row
        # The previous audit may have timed a different set of sources.
        self.calendar.clear_review(row.cik)
        self.calendar.save_profile(row.cik, company)
        return replace(row, company=company)

    async def _check(self, row: Tracked) -> None:
        row = await self._investigate(row)
        if self._defer_if_busy(row.cik):
            return
        self._phase(row.cik, "Looking for the next release date")
        now = self.now()
        unread: list[str] = []
        saved = (
            (row.target, row.evidence)
            if row.target and row.evidence and row.target.window[1] > now
            else None
        )
        # An issuer's word beats FMP's calendar, which beats nothing
        if saved and saved[1] == MANUAL:
            target, evidence = saved
        elif result := await self._issuer_date(row, now, unread):
            target, evidence = result
        elif saved and saved[1] not in (CALENDAR_DATE, CALENDAR_6K_DATE):
            target, evidence = saved
        elif result := await self._calendar_date(row, now):
            target, evidence = result
        elif saved:
            target, evidence = saved
        else:
            seen = (
                f" It saw the notice “{unread[0]}” but couldn't read a date from it."
                if unread
                else ""
            )
            self._phase(
                row.cik,
                f"No release date read yet.{seen} Rechecking hourly. A Manage Server member can "
                "set one with `/track` and `when` and `period`.",
            )
            return
        if self._defer_if_busy(row.cik) or row not in self.calendar.entries():
            return
        if row.target and row.target.ir and same_release(target, row.target):
            target.ir = row.target.ir
        if row.company.sec_form == "6-K" and target.sec_form != "6-K":
            target = target.model_copy(update={"sec_form": "6-K"})
        self._phase(row.cik, None)
        if row.target != target or row.evidence != evidence:
            self.calendar.set_target(target, evidence)
            self.status(f"📅 {describe(target, evidence)}")
        existing_ir = target.ir
        if existing_ir and (
            evidence == MANUAL
            or existing_ir.http_first
            or existing_ir.rss
            or existing_ir.pdfs
            or existing_ir.tesla_deck
            or existing_ir.warmup
            or not existing_ir.verify_text
            or row.cik in self.ir_notes
        ):
            return
        if not target.period:
            self.ir_notes[row.cik] = "The release has no fiscal period to match headlines with"
            self.on_progress()
            return
        listing = existing_ir.listing if existing_ir else issuer_listing(row.ticker, row.company)
        if listing:
            self._phase(row.cik, VERIFYING_IR)
        ir = (
            None
            if evidence == MANUAL
            else await verified_http_source(self.fetcher, listing, target.period)
        )
        if ir:
            why = "Read last quarter's results over direct HTTP"
        elif existing_ir:
            why = "Keeping the previously verified Chrome source"
        else:
            ir, why = await verified_browser_source(self.fetcher, listing, target.period)
        if ir:
            if self._defer_if_busy(row.cik):
                return
            if not any(
                tracked.target == target and tracked.evidence == evidence
                for tracked in self.calendar.entries()
            ):
                self._phase(row.cik, None)
                return
            self.calendar.set_target(target.model_copy(update={"ir": ir}), evidence)
        self.ir_notes[row.cik] = why
        self._phase(row.cik, None)

    def _defer_if_busy(self, cik: int) -> bool:
        if not self.busy():
            return False
        self.checked.pop(cik, None)
        self._phase(cik, "Waiting for an open release window to close")
        return True
