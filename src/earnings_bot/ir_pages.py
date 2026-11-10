"""Read an issuer's IR site the way a person does: open its pages in Chrome, follow its own
navigation to the press releases and events, and collect what's there.

IR sites come in endless layouts, so nothing here knows any one platform. It reads what a
browser shows after the page's scripts have run, which works the same whether the list is in
the HTML, built from JSON, behind a cookie redirect or in a hosted page."""

import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from earnings_bot.browser import BrowserError, RenderedPage, browser_host
from earnings_bot.recorder import ET
from earnings_bot.sources import is_date_notice, looks_like_earnings

Render = Callable[[str], Awaitable[RenderedPage]]

# Navigation that leads to the pages with release dates, best first: press releases, events,
# results, then the IR home a corporate site links as "Investors"
_NAV = (
    re.compile(
        r"(?:all |view all |more |latest |recent )?(?:press|news|financial news)"
        r"(?:[ -]?releases?|room)?(?: (?:and|&) (?:events|media|press|presentations))?"
        r"|(?:corporate |company |investor )?news(?:room)?|media(?: center| centre| room)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:upcoming |investor |all |view all )?(?:events?|calendar|webcasts?)"
        r"(?: (?:and|&) (?:presentations|webcasts|calendar|news))?"
        r"|(?:presentations|news) (?:and|&) events|(?:events?|financial|earnings) calendar",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:quarterly|financial) (?:results|reports|information|earnings)"
        r"|(?:view all )?(?:earnings|results)(?: (?:releases?|reports|center|centre))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"investors?(?: (?:relations|home|overview|center|centre|information))?"
        r"|ir|shareholders?",
        re.IGNORECASE,
    ),
)
EVENTS = 1
RESULTS = 2  # the rank of a "Quarterly Results" link
INVESTORS = 3  # the rank of an "Investors" link
# "WeRide Inc. 2026 Q3 Earnings Conference Call", "Q3 2026 Earnings Webcast"
_PRESS_LINK = re.compile(r"\s+(?:read|view) (?:the )?(?:press|news) release\s*$", re.IGNORECASE)
_EARNINGS_EVENT = re.compile(
    r"^(?=.*\b(?:earnings|results|financial)\b)"
    r"(?=.*\b(?:call|webcast|conference|release|presentation|announcement)\b)"
    r"(?=.*\b(?:q[1-4]|[1-4]q|first|second|third|fourth|full[- ]year|half|interim)\b)",
    re.IGNORECASE,
)
_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAY = re.compile(
    r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b"  # 2026-11-20
    rf"|\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b"  # Nov. 20, 2026
    rf"|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH}\.?,?\s+(20\d{{2}})\b"  # 20 November 2026
    r"|\b(\d{1,2})/(\d{1,2})/(20\d{2})\b",  # 11/20/2026
    re.IGNORECASE,
)
# A month and day without a year, as in Dell's "Conference Call Nov. 24 to Discuss ..."
_YEARLESS = re.compile(
    rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!,?\s*20\d{{2}})", re.IGNORECASE
)
# What comes before a quarter's end rather than an event: "quarter ended September 30, 2026"
_PERIOD_END = re.compile(r"\b(?:ended|ending)\s+(?:on\s+)?$", re.IGNORECASE)
# The time after a date: " 8:00 AM EST", ", at 8:00 a.m. ET", " | 4:30 PM ET"
_CLOCK = re.compile(
    r"[\s,|@\u2022\u2013\u2014-]*(?:at\s+)?(?:approximately\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?"
    r"[\s\u2013\u2014-]*\(?\s*"
    r"(ET|EST|EDT|Eastern(?: Time)?|U\.?S\.? Eastern(?: Time)?|PT|PST|PDT|Pacific"
    r"(?: Time)?|CT|CST|CDT|Central(?: Time)?|MT|MST|MDT|GMT|UTC|BST|CET|CEST|HKT|SGT|"
    r"Hong Kong(?: Time)?|Beijing(?: Time)?|Singapore(?: Time)?)?",
    re.IGNORECASE,
)
_ZONES = {
    "gmt": "UTC",
    "utc": "UTC",
    "bst": "Europe/London",
    "cet": "Europe/Paris",
    "cest": "Europe/Paris",
    "hkt": "Asia/Hong_Kong",
    "hong": "Asia/Hong_Kong",
    "beijing": "Asia/Shanghai",
    "sgt": "Asia/Singapore",
    "singapore": "Asia/Singapore",
    "e": "America/New_York",
    "u": "America/New_York",  # "U.S. Eastern Time"
    "p": "America/Los_Angeles",
    "c": "America/Chicago",
    "m": "America/Denver",
}
_FRAME = re.compile(r"news|press|release|event", re.IGNORECASE)
# A year run into the next word, as a card's "on November 19, 2026November 6, 2026Alibaba"
_GLUED = re.compile(r"(20\d{2})(?=[A-Za-z])")
# A translated copy of the site, as in /zh-hans/news-events/news-releases. Its headlines don't
# read in English, so the reader keeps to the English pages.
_TRANSLATED = re.compile(
    r"/(?:zh|zh-(?:hans|hant|cn|tw|hk)|cn|ja|jp|ko|kr|de|fr|es|it|pt|pt-br|ru|nl|sv|tr|ar|he)"
    r"(?:/|$)",
    re.IGNORECASE,
)


def _zone(name: str | None) -> ZoneInfo:
    if not name:
        return ZoneInfo("America/New_York")  # US-listed issuers' IR sites give Eastern time
    key = name.casefold().replace(".", "").split()[0]
    return ZoneInfo(_ZONES.get(key) or _ZONES.get(key[0]) or "America/New_York")


@dataclass(frozen=True)
class Moment:
    """A date an IR page gives, with its time when it gives one."""

    day: date
    at: datetime | None  # in US Eastern time


def _day(groups: tuple[str | None, ...]) -> date:
    if groups[0]:
        return date(int(groups[0]), int(groups[1] or 0), int(groups[2] or 0))
    if groups[3]:
        return date(
            int(groups[5] or 0), _MONTHS.index(groups[3][:3].lower()) + 1, int(groups[4] or 0)
        )
    if groups[6]:
        return date(
            int(groups[8] or 0), _MONTHS.index((groups[7] or "")[:3].lower()) + 1, int(groups[6])
        )
    return date(int(groups[11] or 0), int(groups[9] or 0), int(groups[10] or 0))


def _timed(text: str, day: date, end: int) -> Moment:
    """The day, with the time that follows it in `text` from `end`, if any."""
    if clock := _CLOCK.match(text, end, end + 40):
        hour, minute, half, zone = clock.groups()
        if 1 <= int(hour) <= 12 and 0 <= int(minute or 0) <= 59:
            local = datetime(
                day.year,
                day.month,
                day.day,
                int(hour) % 12 + (12 if half.lower() == "p" else 0),
                int(minute or 0),
                tzinfo=_zone(zone),
            )
            at = local.astimezone(ET)
            return Moment(at.date(), at)
    return Moment(day, None)


def moments(text: str) -> list[Moment]:
    """Every date in the text, with the time that follows it, if any, as in "2026-11-20
    8:00 AM EST" or "Thursday, November 20, 2026 at 8:00 a.m. ET"."""
    found = []
    text = unglue(text)
    for match in _DAY.finditer(text):
        try:
            day = _day(match.groups())
        except ValueError:
            continue
        found.append(_timed(text, day, match.end()))
    return found


def headline_moments(headline: str, today: date) -> list[Moment]:
    """The dates a headline names for its own event, with or without a year. Dell's
    "Conference Call Nov. 24 to Discuss Third Quarter Fiscal 2027 Financial Results" is on the
    next Nov 24 from today. A date the headline opens with, as Dell's IR home lists it after
    "Nov 10, 2026", is when it was posted, and a quarter's end ("quarter ended September 30,
    2026") isn't the event's day either. Empty when it names no other day."""
    text = unglue(headline).strip()
    if posted := _DAY.match(text):
        text = text[posted.end() :]
    found = []
    for match in [*_DAY.finditer(text), *_YEARLESS.finditer(text)]:
        if _PERIOD_END.search(text[: match.start()]):
            continue
        try:
            if match.re is _DAY:
                day = _day(match.groups())
            else:
                day = date(today.year, _MONTHS.index(match[1][:3].lower()) + 1, int(match[2]))
                if day < today:
                    day = day.replace(year=today.year + 1)
        except ValueError:
            continue
        found.append((match.start(), _timed(text, day, match.end())))
    return [moment for _, moment in sorted(found, key=lambda item: item[0])]


def upcoming(found: list[Moment], today: date, horizon: int = 120) -> list[Moment]:
    """The dates still to come, soonest first."""
    return sorted(
        (moment for moment in found if today <= moment.day <= today + timedelta(days=horizon)),
        key=lambda moment: moment.day,
    )


def unglue(text: str) -> str:
    """Text with each year set apart from a word run into it."""
    return _GLUED.sub(r"\1 ", text)


def _label(text: str) -> str:
    return " ".join(re.sub(r"[^\w&' -]+", " ", text).split()).strip()


def nav_rank(text: str) -> int | None:
    """What a navigation link leads to: 0 press releases, 1 events, 2 results, 3 the IR home."""
    label = _label(text)
    return next((rank for rank, pattern in enumerate(_NAV) if pattern.fullmatch(label)), None)


def earnings_event(title: str) -> bool:
    """An earnings call, webcast or results release, as an IR site's events page names it."""
    return bool(_EARNINGS_EVENT.search(_PRESS_LINK.sub("", title)))


def _site(host: str) -> str:
    """The part of a host that names the company: ir.weride.ai and weride.ai share weride."""
    labels = host.lower().removeprefix("www.").split(".")
    return labels[-2] if len(labels) >= 2 else host


def _plain(url: str) -> str:
    return urlunsplit(urlsplit(url)._replace(fragment=""))


def _public(url: str) -> bool:
    try:
        browser_host(url)
    except ValueError:
        return False
    return True


@dataclass
class IRReading:
    """What the reader saw on an IR site."""

    pages: list[str] = field(default_factory=list)  # read, in order
    notices: list[tuple[str, str]] = field(default_factory=list)  # (url, headline)
    # (url, title, card): the card is the block around the link, often with the date
    events: list[tuple[str, str, str]] = field(default_factory=list)
    results: list[tuple[str, str]] = field(default_factory=list)  # (url, headline)
    listing: str | None = None  # the page that lists its press releases
    home: str | None = None  # the IR site's home, when reached from a corporate site
    results_page: str | None = None  # its "Quarterly Results" page
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """What reading the site came to, as a sentence without its full stop."""
        if not self.pages:
            why = self.problems[0] if self.problems else "it had nothing to open"
            return f"Chrome couldn't open its IR site: {why}"
        found = []
        if self.listing:
            found.append(f"the press release list <{self.listing}>")
        if self.notices:
            found.append(f"{len(self.notices)} date notice(s)")
        if self.events:
            found.append(f"{len(self.events)} earnings event(s)")
        pages = f"Chrome read {len(self.pages)} IR page(s)"
        return f"{pages}, finding {', '.join(found) or 'nothing dated'}"


class IRReader:
    """Reads an IR site through its own navigation, within a page and time budget."""

    def __init__(
        self, render: Render, starts: list[str], *, pages: int = 10, seconds: float = 90
    ) -> None:
        self.render = render
        self.starts = [_plain(url) for url in dict.fromkeys(starts) if _public(url)]
        self.limit = pages
        self.deadline = time.monotonic() + seconds
        self.reading = IRReading()
        self.sites = {_site(urlsplit(url).hostname or "") for url in self.starts}
        self._opened: dict[str, RenderedPage | None] = {}

    def in_budget(self) -> bool:
        return len(self._opened) < self.limit and time.monotonic() < self.deadline

    async def open(self, url: str, rank: int | None = None) -> RenderedPage | None:
        """A page, rendered once; None if Chrome couldn't read it or the budget is spent.
        `rank` is the kind of navigation that led to it, if any."""
        url = _plain(url)
        if url in self._opened:
            return self._opened[url]
        if not self.in_budget():
            return None
        page = None
        try:
            page = await self.render(url)
            if page.status not in (0, 200):
                host = urlsplit(url).hostname
                self.reading.problems.append(f"{host} answered HTTP {page.status}")
                page = None
        except (BrowserError, TimeoutError, ValueError) as error:
            reason = str(error)[:80] or type(error).__name__
            self.reading.problems.append(f"Chrome couldn't open <{url}> ({reason})")
        self._opened[url] = page
        if page is not None:
            self.reading.pages.append(page.url)
            self.sites.add(_site(urlsplit(page.url).hostname or ""))
            if rank is None and page.url != url and self.reading.home is None:
                self.reading.home = page.url
            self._collect(page, rank)
        return page

    def _collect(self, page: RenderedPage, rank: int | None) -> None:
        reading = self.reading
        releases = 0
        cards = page.cards or [text for _, text in page.links]
        for (link, raw), around in zip(page.links, cards, strict=False):
            url, text, card = _plain(link), unglue(raw), unglue(around)
            # A notice names the call too, so a link can be both
            if is_date_notice(text):
                releases += 1
                if (url, text) not in reading.notices:
                    reading.notices.append((url, text))
            if earnings_event(text) and all(seen != url for seen, _, _ in reading.events):
                reading.events.append((url, text, card))
            if looks_like_earnings(text) and len(text) > 25:
                releases += 1
                if (url, text) not in reading.results:
                    reading.results.append((url, text))
        # The press release list: reached through "News Releases" or the like, and listing some
        if rank == 0 and releases >= 2 and reading.listing is None:
            reading.listing = page.url
        if rank == INVESTORS and reading.home is None:
            reading.home = page.url
        if rank == RESULTS and reading.results_page is None:
            reading.results_page = page.url
        if reading.results_page is None:
            reading.results_page = next(
                (url for rank, url in self._links(page) if rank == RESULTS and url != page.url),
                None,
            )

    def _links(self, page: RenderedPage) -> list[tuple[int, str]]:
        """Navigation worth following from a page, best first."""
        found: dict[str, int] = {}
        for link, text in page.links:
            rank = nav_rank(text)
            url = _plain(link)
            path = urlsplit(url).path
            if rank is None or not _public(url) or _TRANSLATED.match(path):
                continue
            if rank == 0 and any(part in path for part in ("/detail/", "/news-details/")):
                continue
            # Stay on the issuer's own sites, though its IR site may be hosted elsewhere
            if rank != INVESTORS and _site(urlsplit(link).hostname or "") not in self.sites:
                continue
            found[url] = min(found.get(url, rank), rank)
        for frame in page.frames:
            if _public(frame) and _FRAME.search(frame):
                found.setdefault(_plain(frame), 0)
        return sorted(((rank, url) for url, rank in found.items()), key=lambda item: item[0])

    async def pages(self) -> AsyncIterator[RenderedPage]:
        """Each page read: the starting pages, then the navigation they lead to."""
        queue: list[tuple[int, str, int | None]] = [(-1, url, None) for url in self.starts]
        while queue and self.in_budget():
            _, url, rank = queue.pop(0)
            if url in self._opened:
                continue
            page = await self.open(url, rank)
            if page is None:
                continue
            yield page
            queued = {item[1] for item in queue}
            queue.extend(
                (found, link, found)
                for found, link in self._links(page)
                if link not in self._opened and link not in queued
            )
            queue.sort(key=lambda item: item[0])
