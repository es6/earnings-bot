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

from earnings_bot.browser import BrowserError, RenderedPage
from earnings_bot.browser_sources import IRSource
from earnings_bot.cache_research import CHECKING_CACHE, CacheResult, CacheRoute, probe_cache
from earnings_bot.calendar import Calendar, Tracked, describe, same_release
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.formatting import clock, day, period_label
from earnings_bot.investigate import OpenChrome, Report, due, investigate
from earnings_bot.ir_pages import (
    EVENTS,
    IRReader,
    IRReading,
    Moment,
    Render,
    earnings_event,
    moments,
    nav_rank,
    upcoming,
)
from earnings_bot.ir_research import (
    chrome_available,
    issuer_links,
    issuer_listing,
    prior_period,
    quarter_headline,
    verified_browser_source,
    verified_http_source,
    verified_news_source,
    verified_results_table,
)
from earnings_bot.issuer_news import news_records, read_news
from earnings_bot.known import (
    KnownCompany,
    fiscal_period,
    known_companies,
    to_target,
)
from earnings_bot.periods import title_period
from earnings_bot.quarterly_table import quarter_pdf
from earnings_bot.recorder import ET, PollError, Target, poll
from earnings_bot.sec_results import SixKResults, SixKUnavailable
from earnings_bot.source_audit import Speed, published_first
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
# An event the IR site hasn't settled yet, as Lowe's "Earnings Conference Call (tentative)"
_TENTATIVE = re.compile(r"\b(?:tentative|tbc|tbd|to be confirmed|estimated)\b", re.IGNORECASE)
EVENT_DATE = "Issuer's earnings event"
READING_IR = "Reading its IR site in Chrome"
RECHECKING = "Rechecking hourly."
IR_REREAD = 6 * 3600  # seconds
RESEARCH_SECONDS = 450  # for one company's hourly check
VERIFYING_IR = "Verifying the issuer's last report"
CACHE_REFRESH = 24 * 3600
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
    r"(am|pm)\s*\(?(?:US\s+)?(ET|EST|EDT|Eastern(?: Time)?|PT|PST|PDT|Pacific(?: Time)?)\b",
    re.IGNORECASE,
)
_MARKET = r"(?:financial )?markets?"
_AFTER = re.compile(
    rf"after (?:the )?(?:(?:US|U S) )?"
    rf"(?:{_MARKET} closes?|closing bell|close of (?:the )?{_MARKET})\b",
    re.IGNORECASE,
)
_BEFORE = re.compile(
    rf"(?:before|prior to) (?:the )?(?:(?:US|U S) )?"
    rf"(?:{_MARKET} opens?|opening bell|open(?:ing)? of (?:the )?{_MARKET}|open\b)"
    # TJX's "before 9:30 am ET", the open by the clock
    r"|before 9:30 ?am\b",
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


def _tidy(line: str) -> str:
    """A line whose abbreviations won't end a sentence: "Nov. 12", "2 p.m." and "U.S."."""
    line = _full_months(" ".join(line.split()))
    return re.sub(r"\b([ap])\.m\.", r"\1m", line, flags=re.IGNORECASE).replace("U.S.", "US")


def parse_notice(
    ticker: str, company: KnownCompany, title: str, body: str, now: datetime
) -> tuple[Target, str] | None:
    if not is_date_notice(title):
        return None

    title = _tidy(title)
    # Kept apart, so a heading or the page's "posted at" line doesn't run into the next line
    lines = [_tidy(line) for line in html_to_text(_BREAK.sub(" ", body)).splitlines()]
    # The headline's explicit fiscal period takes precedence over other quarters in the page.
    period = title_period(title) or title_period(" ".join(lines))
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
    # A notice that names no quarter it can be read from ("will release its financial results
    # for the fourth quarter and fiscal year ended September 30") reports the one that ended last
    if period is None and company.fiscal_year_end:
        period = fiscal_period(company.fiscal_year_end, day)
    if period is None:
        return None
    year, quarter = period
    expected = next((stamp for s in release if (stamp := _release_time(s, day))), None)
    if expected is not None:
        if expected <= now:
            return None
        target = to_target(ticker, expected, year, quarter, company)
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
    if (mode == "after market close" and hour < 16) or (
        mode == "before market open" and (hour, minute) >= (9, 30)
    ):
        hour, minute = map(int, fallback.split(":"))
    expected = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
    estimate = expected
    call_deadline = None
    if mode != "after market close":
        # The issuer's same-day earnings call is a deadline for results, even when the
        # historical SEC acceptance times would put the estimate later than the call, as for
        # Warner Music's "will hold an earnings conference call that morning at 8:30 a.m."
        for sentence in sentences:
            if (
                _CALL.search(sentence)
                and (_SAME_DAY.search(sentence) or day in _dates(sentence, today))
                and (call := _release_time(sentence, day, allow_call=True))
                and (mode != "before market open" or call <= expected.replace(hour=9, minute=30))
            ):
                call_deadline = min(call_deadline, call) if call_deadline else call
        if call_deadline and call_deadline <= expected:
            # Before the call, by as much as an hour when the time was only estimated
            expected = (
                call_deadline
                if mode == "before market open"
                else call_deadline - timedelta(hours=1)
            )
    target = to_target(ticker, expected, year, quarter, company)
    target.before_minutes = 90 if expected < estimate else 60
    target.after_minutes = 90
    if call_deadline and call_deadline >= expected:
        target.after_minutes = max(90, (call_deadline - expected) // timedelta(minutes=1) + 5)
    if mode == "before market open":
        # Before the open can be any time from early morning to 9:30, whatever past filing
        # times say: TJX's 8-Ks came at 9:10 while its notice says only "before 9:30 a.m."
        opens = expected.replace(hour=9, minute=35)
        early = expected.replace(hour=6, minute=0)
        target.after_minutes = max(target.after_minutes, (opens - expected) // timedelta(minutes=1))
        target.before_minutes = max(
            target.before_minutes,
            (expected - early) // timedelta(minutes=1),
            90 if company.sec_form == "6-K" and not company.release_published else 0,
        )
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
    if api := company.ir_news_api:
        with suppress(aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            reply = await read_news(fetcher, api)
            if reply.status == 200:
                for row in news_records(api, reply.body):
                    if result := parse_notice(
                        ticker, company, row.title, escape(row.description), now
                    ):
                        target, evidence = result
                        return target, f"{evidence} <{row.url}>"
                    if is_date_notice(row.title):
                        unread.append(row.title)
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


async def _wire_date(
    fetcher: Fetcher, ticker: str, company: KnownCompany, now: datetime
) -> tuple[Target, str] | None:
    """An issuer-authored notice can be public on its wire even when IR is unreadable."""
    if company.wire not in ("PR Newswire", "unknown"):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", company.name.casefold()).strip("-")
    if not slug:
        return None
    exchange = re.compile(rf"\b(?:NASDAQ|NYSE|TSX|AMEX)\s*:\s*{re.escape(ticker)}\b", re.IGNORECASE)
    for variant in (slug, slug + "."):
        url = f"https://www.prnewswire.com/news/{variant}/"
        with suppress(aiohttp.ClientError, TimeoutError, ValueError):
            listing = await fetcher.request("GET", url, give_up_after=4, allow_redirects=False)
            if listing.status != 200:
                continue
            notices = [
                (link, title)
                for link, title in issuer_links(url, listing.body)
                if urlsplit(link).path.startswith("/news-releases/") and is_date_notice(title)
            ]
            for link, title in notices[:3]:
                page = await fetcher.request("GET", link, give_up_after=4, allow_redirects=False)
                if page.status != 200:
                    continue
                html = page.body.decode(errors="replace")
                if not exchange.search(html_to_text(html)):
                    continue
                if result := parse_notice(ticker, company, title, html, now):
                    target, evidence = result
                    # The publisher of the notice is also an available race source.
                    target.prnewswire = company.names
                    return target, f"{evidence} <{link}>"
    return None


def timed_by_publishers(company: KnownCompany, review: str | None) -> KnownCompany:
    """The company with its usual release time taken from when its IR site or wire had the
    last release, when that was before SEC had it."""
    speed = Speed.from_json(review)
    first = published_first(speed) if speed else None
    if first is None:
        return company
    local = first.astimezone(ET)
    release = f"{local.hour:02d}:{local.minute - local.minute % 5:02d}"
    return company.model_copy(update={"release_time": release, "release_published": True})


def _weekday_before(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _period_of(title: str, text: str, company: KnownCompany, day: date) -> tuple[int, int] | None:
    """The period an event or notice is for: the one it names, or else the quarter that ended
    last before its date, as "John Deere - 4Q Earnings Call" in November is."""
    if period := title_period(title) or title_period(text[:3000]):
        return period
    return fiscal_period(company.fiscal_year_end, day) if company.fiscal_year_end else None


def event_target(
    ticker: str, company: KnownCompany, title: str, moment: Moment, now: datetime, text: str = ""
) -> tuple[Target, str] | None:
    """The release an IR site's earnings event implies. Results come out before the call: the
    same morning or afternoon, or the evening before a morning call."""
    if _TENTATIVE.search(title) or _TENTATIVE.search(text[:400]):
        return None  # a placeholder, not a date
    period = _period_of(title, text, company, moment.day)
    if period is None:
        return None
    year, quarter = period
    history = company.release_time
    hour, minute = map(int, history.split(":")) if history else (None, None)
    call = moment.at
    when = (
        f"{call:%a, %b} {call.day} at {clock(call)}"
        if call
        else f"{moment.day:%a, %b} {moment.day.day}"
    )
    if call is None:
        if hour is None or minute is None:
            return None
        expected = datetime(
            moment.day.year, moment.day.month, moment.day.day, hour, minute, tzinfo=ET
        )
        before, after = 60, 90
    elif (
        call.hour < 12
        and hour is not None
        and minute is not None
        and hour >= 16
        and (company.sec_form != "6-K" or company.release_published)
    ):
        # Past 8-Ks came out after the close, so a morning call follows an evening release
        day = _weekday_before(call.date())
        expected = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
        before, after = 60, 90
    else:
        same_day = (
            call.replace(hour=hour, minute=minute)
            if hour is not None and minute is not None
            else None
        )
        if (
            same_day
            and same_day < call
            and (call - same_day <= timedelta(hours=3) or (same_day.hour < 12 and call.hour < 12))
            and (company.sec_form != "6-K" or company.release_published)
        ):
            expected = same_day
            before = 60
        else:
            # A 6-K can be filed hours after the release, so the call is the better guide
            expected = call - timedelta(hours=1)
            before = 90
        after = (call - expected) // timedelta(minutes=1) + 15
    target = to_target(ticker, expected, year, quarter, company)
    target.before_minutes, target.after_minutes = before, after
    if target.window[1] <= now:
        return None
    return target, f"{EVENT_DATE}: “{title}”, {when}"


async def rendered_date(
    render: Render,
    starts: list[str],
    ticker: str,
    company: KnownCompany,
    now: datetime,
    unread: list[str],
) -> tuple[tuple[Target, str] | None, IRReading]:
    """Read the IR site in Chrome, through its own navigation, for the issuer's date notice or
    else its next earnings event. Works for any layout, including lists built by scripts."""
    reader = IRReader(render, starts)
    today = now.astimezone(ET).date()
    tried: set[str] = set()
    # Earnings calls the site lists: (title, when, where, the text around it)
    events: list[tuple[str, Moment, str, str]] = []
    async for page in reader.pages():
        # A notice dated ahead in its headline is surely this quarter's, so it goes first
        notices = sorted(
            reader.reading.notices, key=lambda notice: not upcoming(moments(notice[1]), today)
        )
        for url, title in notices:
            # A notice whose headline gives a date that has passed is an old one
            dated = moments(title)
            # A link back to a page already read, as HPE's "/#11-23-2026", isn't a notice page
            if (
                url in tried
                or url in reader.reading.pages
                or len(tried) >= 4
                or (dated and not upcoming(dated, today))
            ):
                continue
            tried.add(url)
            notice = await reader.open(url)
            if notice is None:
                continue
            if result := parse_notice(ticker, company, title, escape(notice.text), now):
                target, evidence = result
                return (target, f"{evidence} <{url}>"), reader.reading
            # A notice of the call alone still dates the release, which comes before it
            if call := _call_moment(notice.text, today):
                events.append((title, call, url, notice.text))
            elif title not in unread:
                unread.append(title)
        events.extend(_listed_events(page, today))
    # Each event link's card usually shows its date beside the title
    events.extend(
        (title, moment, url, card)
        for url, title, card in reader.reading.events
        for moment in upcoming(moments(card), today)[:1]
    )
    # An event's own page gives its date when the list doesn't
    for url, title, _ in reader.reading.events[: 0 if events else 2]:
        if page := await reader.open(url):
            dates = upcoming(moments(page.text[:5000]), today)
            events.extend((title, moment, url, page.text) for moment in dates[:1])
    for title, moment, url, text in sorted(events, key=lambda item: item[1].day):
        if result := event_target(ticker, company, title, moment, now, text):
            target, evidence = result
            return (target, f"{evidence} <{url}>"), reader.reading
    return None, reader.reading


def _call_moment(text: str, today: date) -> Moment | None:
    """When a notice says its earnings call is, as in Autodesk's "conference call ... on
    Tuesday, November 24, 2026, at 2 p.m. Pacific Time (PT)"."""
    found = [
        moment
        for sentence in _SENTENCE.split(_tidy(text))
        if _CALL.search(sentence)
        for moment in upcoming(moments(sentence), today)
    ]
    timed = [moment for moment in found if moment.at]
    return (timed or found or [None])[0]


def _listed_events(page: RenderedPage, today: date) -> list[tuple[str, Moment, str, str]]:
    """Earnings events a page lists with their dates beside them, as in "WeRide Inc. 2026 Q3
    Earnings Conference Call 2026-11-20 8:00 AM EST"."""
    found = []
    lines = [line.strip() for line in page.text.splitlines() if line.strip()]
    event_page = re.search(r"(?:events?|calendar)", urlsplit(page.url).path, re.IGNORECASE)
    for number, line in enumerate(lines):
        nearby = lines[max(0, number - 3) : number]
        dated_results = (
            (event_page or any(nav_rank(heading) == EVENTS for heading in nearby))
            and len(line) <= 160
            and re.search(r"\b(?:results|earnings)$", line, re.IGNORECASE)
            and title_period(line)
        )
        if not (earnings_event(line) or dated_results):
            continue
        # Some event cards put a standalone date immediately before the title.
        adjacent = [
            lines[i]
            for i in (number + 1, number - 1)
            if 0 <= i < len(lines) and len(lines[i]) <= 60
        ]
        for entry in (line, *adjacent):
            if dated := upcoming(moments(entry), today):
                found.append((line, dated[0], page.url, f"{line} {entry}"))
                break
    return found


def ir_starts(ticker: str, company: KnownCompany) -> list[str]:
    """Where to start reading an issuer's IR site."""
    origins = [
        f"https://{urlsplit(url).netloc}/"
        for url in (company.ir_listing, company.ir_news_api, *company.rss)
        if url
    ]
    if company.q4_host:
        origins.append(f"https://{company.q4_host}/")
    starts = [company.ir_home, issuer_listing(ticker, company), *origins]
    if company.ir_home and not origins and not company.ir_listing:
        # A company's release may give only its retail website. Try standard IR subdomains
        # of that same corporate domain before giving up on the issuer's calendar.
        host = urlsplit(company.ir_home).hostname or ""
        if host.startswith("www."):
            domain = host.removeprefix("www.")
            starts += [f"https://{prefix}.{domain}/" for prefix in ("investor", "investors", "ir")]
    return [url for url in dict.fromkeys(starts) if url]


def _cache_routes(row: Tracked) -> list[CacheRoute]:
    target, company = row.target, row.company
    ir = target.ir if target else None
    api = (ir.news_api if ir else None) or company.ir_results_api or company.ir_news_api
    q4_host = target.q4_host if target else company.q4_host
    routes: list[CacheRoute] = []
    if api:
        label = "IR results API" if api == company.ir_results_api else "IR newsroom API"
        routes.append(CacheRoute(label, api))
    if ir and ir.results_table and not ir.table_rendered and target and target.period:
        routes.append(
            CacheRoute(
                "IR quarterly results table",
                ir.results_table,
                warmup=None if ir.table_http else ir.results_table,
                revalidate=ir.table_http,
                table_period=prior_period(target.period),
            )
        )
    if q4_host:
        q4 = Q4PressReleases(q4_host).url().rsplit("&_=", 1)[0]
        routes.append(CacheRoute("Q4 press releases", q4))
    if ir and ir.rss:
        warmup = ir.warmup or ir.listing or urljoin(ir.rss, "/")
        routes.append(CacheRoute("Issuer RSS in Chrome", ir.rss, warmup=warmup))
    if ir and ir.listing:
        routes.append(
            CacheRoute(
                "IR press release listing",
                ir.listing,
                warmup=None if ir.http_first else ir.listing,
                revalidate=ir.http_first,
            )
        )
    feeds = [*target.rss, *target.trial.rss] if target else [*company.rss, *company.trial.rss]
    routes.extend(
        CacheRoute(f"Issuer RSS on {urlsplit(url).hostname}", url) for url in dict.fromkeys(feeds)
    )
    if not routes and (listing := issuer_listing(row.ticker, company)):
        warmup = listing if company.ir_http_status != 200 and chrome_available() else None
        routes.append(
            CacheRoute("IR press release listing", listing, warmup=warmup, revalidate=True)
        )
    return routes[:3]


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
        self.cache_notes: dict[int, list[str]] = {}
        # What reading each company's IR site in Chrome found, and when it last ran
        self.ir_reads: dict[int, str] = {}
        self.ir_read_at: dict[int, float] = {}
        self.cache_results: dict[CacheRoute, tuple[float, CacheResult]] = {}
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
            # Room for a first investigation and a read of the IR site, both through Chrome
            async with asyncio.timeout(RESEARCH_SECONDS):
                await self._check(row)
                if current := self.calendar.find(row.ticker):
                    await self._prepare_cache(current)
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
        if row.company.sec_form == "6-K":
            with suppress(SixKUnavailable):
                async for notice in self.sixk.notices(row.cik, now):
                    if result := parse_notice(
                        row.ticker, row.company, notice.headline, escape(notice.text), now
                    ):
                        target, evidence = result
                        return target, f"{evidence} from issuer 6-K exhibit <{notice.url}>"
        result = await self._ir_site_date(row, now, unread)
        if result or (
            row.target
            and row.target.window[1] > now
            and row.evidence not in (CALENDAR_DATE, CALENDAR_6K_DATE)
        ):
            return result
        return await _wire_date(self.fetcher, row.ticker, row.company, now)

    async def _ir_site_date(
        self, row: Tracked, now: datetime, unread: list[str]
    ) -> tuple[Target, str] | None:
        """The date from the IR site as Chrome shows it, when its feeds and lists didn't give
        one. A date already found this way is read again every few hours, not hourly."""
        open_chrome = self.chrome or (self.fetcher.research_browser if chrome_available() else None)
        starts = ir_starts(row.ticker, row.company)
        if open_chrome is None or not starts or self.busy():
            return None
        if (
            row.target
            and row.evidence
            and row.target.window[1] > now
            and time.monotonic() < self.ir_read_at.get(row.cik, 0) + IR_REREAD
        ):
            return row.target, row.evidence
        self._phase(row.cik, READING_IR)
        try:
            async with open_chrome() as browser:

                async def render(url: str) -> RenderedPage:
                    if self.busy():
                        raise TimeoutError
                    return await browser.render(url, 15, self.fetcher.wait_turn)

                result, reading = await rendered_date(
                    render, starts, row.ticker, row.company, now, unread
                )
        except (BrowserError, aiohttp.ClientError, TimeoutError, ValueError) as error:
            self.ir_reads[row.cik] = f"Chrome couldn't read its IR site ({type(error).__name__})"
            self.on_progress()
            return None
        self.ir_read_at[row.cik] = time.monotonic()
        self.ir_reads[row.cik] = f"{reading.summary()}."
        self.on_progress()
        return result

    def _phase(self, cik: int, text: str | None) -> None:
        if text is None:
            self.phases.pop(cik, None)
        else:
            self.phases[cik] = text
        self.on_progress()

    async def _probe_cache(self, route: CacheRoute) -> CacheResult:
        signature = None
        if period := route.table_period:

            def table_signature(body: bytes) -> str | None:
                return quarter_pdf(route.url, body, *period)

            signature = table_signature

        async def get(url: str) -> Response:
            if self.busy():
                raise TimeoutError
            headers = {"Cache-Control": "no-cache"} if route.revalidate else None
            return await self.fetcher.request(
                "GET", url, headers, give_up_after=4, allow_redirects=False
            )

        if route.warmup is None:
            return await probe_cache(get, route.url, signature)
        open_chrome = self.chrome or (self.fetcher.research_browser if chrome_available() else None)
        if open_chrome is None:
            return CacheResult("Desktop Chrome unavailable")
        warmup = route.warmup
        try:
            async with asyncio.timeout(30), open_chrome() as browser:

                async def get_browser(url: str) -> Response:
                    if self.busy():
                        raise TimeoutError
                    return await browser.request(url, warmup, 4, self.fetcher.wait_turn)

                return await probe_cache(get_browser, route.url, signature)
        except (BrowserError, aiohttp.ClientError, TimeoutError, ValueError):
            return CacheResult("Chrome cache comparison failed")

    async def _prepare_cache(self, row: Tracked) -> None:
        if self.busy():
            self.checked.pop(row.cik, None)
            return
        target, evidence = row.target, row.evidence
        ir = target.ir if target else None
        bust = set(ir.cache_bust_urls if ir else ())
        canonical = set(target.rss_canonical if target else ())
        notes: list[str] = []
        if ir and ir.table_rendered:
            notes.append("IR quarterly results page · Chrome navigation cache freshness unverified")
        prior_phase = self.phases.get(row.cik)
        try:
            for route in _cache_routes(row):
                if self.busy():
                    self.checked.pop(row.cik, None)
                    return
                saved = self.cache_results.get(route)
                if saved and saved[0] > time.monotonic():
                    result = saved[1]
                else:
                    if self.phases.get(row.cik) != CHECKING_CACHE:
                        self._phase(row.cik, CHECKING_CACHE)
                    try:
                        async with asyncio.timeout(35):
                            result = await self._probe_cache(route)
                    except TimeoutError:
                        result = CacheResult("Cache comparison timed out")
                    if self.busy():
                        self.checked.pop(row.cik, None)
                        return
                    self.cache_results[route] = time.monotonic() + CACHE_REFRESH, result
                notes.append(f"{route.label} · {result.summary}")
                if ir and route.url in (ir.news_api, ir.listing, ir.rss, ir.results_table):
                    if result.query_bust:
                        bust.add(route.url)
                    elif result.canonical_only or result.stored_only:
                        bust.discard(route.url)
                if target and (route.url in target.rss or route.url in target.trial.rss):
                    # A query that gets the same stored copy only costs the cache a lookup
                    if result.canonical_only or result.stored_only:
                        canonical.add(route.url)
                    elif result.query_bust:
                        canonical.discard(route.url)
            if target and evidence and self.calendar.find(row.ticker) == row:
                updated = target.model_copy(
                    update={
                        "ir": ir.model_copy(update={"cache_bust_urls": sorted(bust)})
                        if ir
                        else None,
                        "rss_canonical": sorted(canonical),
                    }
                )
                if updated != target:
                    self.calendar.set_target(updated, evidence)
            updated_notes = notes or ["No issuer endpoint available to test"]
            if self.cache_notes.get(row.cik) != updated_notes:
                self.cache_notes[row.cik] = updated_notes
                self.on_progress()
        finally:
            if self.phases.get(row.cik) == CHECKING_CACHE:
                self._phase(row.cik, prior_phase)

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
        # Place windows by when the release came out last time, not when SEC had it
        review = self.calendar.review(row.cik)[1]
        timed = replace(row, company=timed_by_publishers(row.company, review))
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
        elif result := await self._issuer_date(timed, now, unread):
            target, evidence = result
        elif saved and saved[1] not in (CALENDAR_DATE, CALENDAR_6K_DATE):
            target, evidence = saved
        elif result := await self._calendar_date(timed, now):
            target, evidence = result
        elif saved:
            target, evidence = saved
        else:
            today = now.astimezone(ET).date()
            # The release that came and went, whose own notice isn't one for the next
            done = row.target if row.target and row.target.window[1] <= now else None
            last_day = done.expected.astimezone(ET).date() if done else None
            # A notice whose headline dates a release that has passed is an old one
            fresh = [
                t
                for t in unread
                if (
                    not (dated := moments(t))
                    and not (
                        done
                        and (named := title_period(t))
                        and done.period == f"FY{named[0]}Q{named[1]}"
                    )
                )
                or (
                    upcoming(dated, today)
                    and not (last_day and max(m.day for m in dated) <= last_day)
                )
            ]
            if fresh:
                seen = f" It saw the notice “{fresh[0]}” but couldn't read a date from it."
            elif unread and not done:
                seen = f" Its latest date notice, “{unread[0]}”, is for an earlier release."
            else:
                seen = ""
            read = f" {self.ir_reads[row.cik]}" if row.cik in self.ir_reads else ""
            if done:
                quarter = period_label(done.period) or "Its latest"
                speed = Speed.from_json(review)
                confirmed = self.calendar.delivered(done.release_key) or bool(
                    speed and speed.confident and speed.day == last_day
                )
                if confirmed:
                    status = (
                        f"{quarter} results came out {day(done.expected)}. The next release "
                        "isn't announced yet."
                    )
                else:
                    status = (
                        f"{quarter} watch ended {day(done.expected)} without a confirmed release."
                    )
            else:
                status = "No release date read yet."
            self._phase(
                row.cik,
                f"{status}{read}{seen} {RECHECKING} A Manage Server "
                "member can set one with `/track` and `when` and `period`.",
            )
            return
        if self._defer_if_busy(row.cik) or row not in self.calendar.entries():
            return
        if row.target and same_release(target, row.target):
            if row.target.ir:
                target.ir = row.target.ir
            feeds = {*target.rss, *target.trial.rss}
            target.rss_canonical = [url for url in row.target.rss_canonical if url in feeds]
        if row.company.sec_form == "6-K" and target.sec_form != "6-K":
            target = target.model_copy(update={"sec_form": "6-K"})
        self._phase(row.cik, None)
        if row.target != target or row.evidence != evidence:
            self.calendar.set_target(target, evidence)
            self.status(f"📅 {describe(target, evidence)}")
        existing_ir = target.ir
        results_api = row.company.ir_results_api
        table = row.company.ir_results_page
        browser_feed = row.company.ir_browser_rss
        newer = bool(results_api and existing_ir and existing_ir.news_api != results_api)
        extras_due = row.cik not in self.ir_notes and (
            (table and (not existing_ir or existing_ir.results_table != table))
            or (browser_feed and (not existing_ir or existing_ir.rss != browser_feed))
        )
        if (
            existing_ir
            and not newer
            and not extras_due
            and (
                evidence == MANUAL
                or existing_ir.news_api
                or existing_ir.http_first
                or existing_ir.rss
                or existing_ir.pdfs
                or existing_ir.tesla_deck
                or existing_ir.warmup
                or not existing_ir.verify_text
                or row.cik in self.ir_notes
            )
        ):
            return
        if not target.period:
            self.ir_notes[row.cik] = "The release has no fiscal period to match headlines with"
            self.on_progress()
            return
        listing = existing_ir.listing if existing_ir else issuer_listing(row.ticker, row.company)
        if listing:
            self._phase(row.cik, VERIFYING_IR)
        ir = await verified_news_source(self.fetcher, results_api, target.period)
        if ir:
            why = "Read last quarter's press release PDF from the issuer's results API"
        elif ir := await verified_news_source(self.fetcher, row.company.ir_news_api, target.period):
            why = "Read last quarter's report PDF from the issuer newsroom API"
        else:
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
        # Keep the verified routes together. The table is independent of a newsroom API or RSS.
        if existing_ir and ir:
            routes = {
                **existing_ir.model_dump(exclude_defaults=True, exclude={"cache_bust_urls"}),
                **ir.model_dump(exclude_defaults=True, exclude={"cache_bust_urls"}),
            }
            urls = {routes.get(field) for field in ("listing", "news_api", "rss", "results_table")}
            ir = IRSource.model_validate(
                {
                    **routes,
                    "cache_bust_urls": [url for url in existing_ir.cache_bust_urls if url in urls],
                }
            )
        elif existing_ir:
            ir = existing_ir
        additions: dict[str, str | bool] = {}
        if table and (not ir or ir.results_table != table):
            additions.update(await verified_results_table(self.fetcher, table, target.period))
        if browser_feed and (not ir or not ir.rss) and chrome_available():
            additions.update(
                rss=browser_feed,
                headline=quarter_headline(int(target.period[2:6]), int(target.period[-1])),
            )
            if ir and ir.listing and urlsplit(ir.listing).netloc != urlsplit(browser_feed).netloc:
                additions["warmup"] = urljoin(browser_feed, "/")
        if additions:
            ir = IRSource.model_validate({**(ir.model_dump() if ir else {}), **additions})
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
