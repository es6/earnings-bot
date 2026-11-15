"""Guesses GlobeNewswire's next article numbers around a GlobeNewswire issuer's release times,
and alerts from its article page as soon as the page opens.

GlobeNewswire numbers its articles in publish order (50 releases, none out of order) and opens
an article by its number with any ending on rss.globenewswire.com. The first pages of a
scheduled batch opened 9 to 13 s after its time, 10 to 40 s before GlobeNewswire's feeds listed
them; AMAT's IR site had its release 44 s after GlobeNewswire (all measured Nov 12). A number
not out yet answers 410, which shared caches may keep for 30 days, so every guess ends in a
name never asked before: a guess that comes too early can't hide the article later.

The rate is the user's (Nov 14): three numbers at once every 2 s, 1.5 requests a second, at most
540 guesses a release over 6 minutes split among its release times, and none after GlobeNewswire
refuses one.
"""

import asyncio
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from earnings_bot.fetch import Fetcher
from earnings_bot.periods import agrees_with_period
from earnings_bot.release_band import ET
from earnings_bot.sec_results import announces_results
from earnings_bot.sources import RssFeed, Sighting, results_headline
from earnings_bot.text import html_to_text

if TYPE_CHECKING:  # the recorder starts the guessing, so it can't be imported from here
    from earnings_bot.recorder import Journal, Target

# Public companies' releases, newest first: where the guessing starts
FEED = "https://rss.globenewswire.com/RssFeed/orgclass/1"
PAGE = "https://rss.globenewswire.com/news-release/{day}/{number}/0/en/{ending}.html"
AHEAD = 3  # numbers guessed at once
ROUND = 2.0  # seconds between rounds
BURST = 6  # numbers opened at once when every guess was already out
BUDGET = 540  # guesses a release
SPAN = timedelta(minutes=6)  # of guessing a release, split among its release times
BEFORE = timedelta(minutes=2)  # the most of each share spent before the release time
STALE = 10  # a number this far below the newest open one is given up on
REFUSED = frozenset({403, 429, 500, 502, 503, 504})

_NUMBER = re.compile(r"/news-release/\d{4}/\d{2}/\d{2}/(\d+)/")
_TICKERS = re.compile(r'<meta name="ticker" content="([^"]*)"', re.IGNORECASE)
_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"', re.IGNORECASE)
_PUBLISHED = re.compile(r'"datePublished":"([^"]+)"')


@dataclass(frozen=True)
class Page:
    number: int
    url: str  # on www, as GlobeNewswire's feeds link it, so it's read like a feed item
    title: str
    tickers: str  # "Nasdaq:WRD, HKEX:0800"
    published: str | None
    body: bytes


class Refused(Exception):
    """GlobeNewswire refused a guess: no more guessing this release."""


def spans(times: Sequence[datetime]) -> list[tuple[datetime, datetime]]:
    """When to guess for a release: 6 minutes split among its release times, each share from up
    to 2 minutes before its time (NVIDIA's one time: 16:18 to 16:24)."""
    ordered = sorted(times)
    share = SPAN / len(ordered)
    before = min(BEFORE, share / 3)
    return [(moment - before, moment - before + share) for moment in ordered]


def guessed_for(target: "Target") -> bool:
    """A release on GlobeNewswire alone: its GlobeNewswire names are set, and no other wire's
    are, as they all are for a company whose wire isn't known."""
    return bool(target.trial.globenewswire) and not target.newswire and not target.prnewswire


class Frontier:
    """Guesses the numbers after GlobeNewswire's newest, one release's budget at a time."""

    def __init__(self, fetcher: Fetcher, journal: "Journal", label: str, budget: int = BUDGET):
        self.fetcher = fetcher
        self.journal = journal
        self.label = label  # a ticker, or a batch's id
        self.budget = budget
        self.sent = 0
        self._name = secrets.token_hex(4)

    def _url(self, number: int) -> str:
        self.sent += 1
        day = self.journal.now().astimezone(ET)  # GlobeNewswire ignores the date in the path
        ending = f"{self._name}-{self.sent:04d}"
        return PAGE.format(day=f"{day:%Y/%m/%d}", number=number, ending=ending)

    async def newest(self) -> int | None:
        """The newest article number GlobeNewswire's public-company feed lists."""
        url = f"{FEED}?{secrets.token_hex(4)}"  # a copy no cache has
        try:
            response = await self.fetcher.request("GET", url, give_up_after=10)
        except (OSError, TimeoutError, ValueError) as error:
            self.journal.write("globe_guess_error", label=self.label, message=repr(error))
            return None
        numbers = [int(n) for n in _NUMBER.findall(response.body.decode(errors="replace"))]
        if response.status != 200 or not numbers:
            self.journal.write("globe_guess_error", label=self.label, status=response.status)
            return None
        return max(numbers)

    async def guess(self, number: int) -> Page | None:
        """The article with this number, or None while it isn't out. Raises Refused."""
        url = self._url(number)
        started = time.monotonic()
        try:
            response = await self.fetcher.request("GET", url, give_up_after=5)
        except (OSError, TimeoutError, ValueError) as error:
            self.journal.write("globe_guess", label=self.label, number=number, error=repr(error))
            return None
        self.journal.write(
            "globe_guess",
            label=self.label,
            number=number,
            status=response.status,
            ms=round((time.monotonic() - started) * 1000),
        )
        if response.status in REFUSED:
            raise Refused(f"{response.status} for article {number}")
        if response.status != 200:
            return None
        head = response.body[:60_000].decode(errors="replace")
        title = unescape(found[1]) if (found := _TITLE.search(head)) else ""
        tickers = unescape(found[1]) if (found := _TICKERS.search(head)) else ""
        published = found[1] if (found := _PUBLISHED.search(head)) else None
        page = Page(
            number,
            url.replace("https://rss.", "https://www.", 1),
            title,
            tickers,
            published,
            response.body,
        )
        self.journal.write(
            "globe_page",
            label=self.label,
            number=number,
            title=title,
            tickers=tickers,
            published=published,
        )
        return page

    async def watch(self, stop: datetime, found: Callable[[Page], Awaitable[bool]]) -> str:
        """Guesses until `stop`, the budget runs out, `found` says a page is the one, or
        GlobeNewswire refuses: "time", "budget", "found" or "refused"."""
        base = await self.newest()
        if base is None:
            return "time"
        opened: set[int] = set()
        count = AHEAD
        while self.journal.now() < stop and self.sent < self.budget:
            started = time.monotonic()
            top = max(opened, default=base)
            base = max(base, top - STALE)
            while base + 1 in opened:
                base += 1
            numbers: list[int] = []
            number = base
            while len(numbers) < min(count, self.budget - self.sent):
                number += 1
                if number not in opened:
                    numbers.append(number)
            try:
                pages = [p for p in await asyncio.gather(*map(self.guess, numbers)) if p]
            except Refused as error:
                self.journal.write("globe_refused", label=self.label, message=str(error))
                return "refused"
            for page in sorted(pages, key=lambda page: page.number):
                opened.add(page.number)
                if await found(page):
                    return "found"
            # Every guess was out: the batch is ahead of us, so open the next ones at once
            count = BURST if len(pages) == len(numbers) else AHEAD
            if count == AHEAD:
                await asyncio.sleep(max(0.0, ROUND - (time.monotonic() - started)))
        return "budget" if self.sent >= self.budget else "time"


def is_release(page: Page, target: "Target") -> bool:
    """The target's quarterly results: its ticker on the page, and a results headline for its
    quarter or a text that announces them (sec_results.announces_results)."""
    symbols = {part.split(":")[-1].strip().upper() for part in page.tickers.split(",")}
    if target.ticker.upper() not in symbols:
        return False
    if results_headline(page.title) and agrees_with_period(page.title, target.period):
        return True
    return announces_results(html_to_text(page.body.decode(errors="replace")), target.period)


async def guess_release(
    target: "Target",
    fetcher: Fetcher,
    journal: "Journal",
    notify: Callable[[RssFeed, Sighting], None] | None,
    end: datetime,
) -> None:
    """Guesses a GlobeNewswire issuer's article around each of its release times, and posts the
    page that's its results. Never raises: the window's other routes go on."""
    source = RssFeed("globenewswire_page", FEED, target.trial.globenewswire)
    frontier = Frontier(fetcher, journal, target.ticker)

    async def found(page: Page) -> bool:
        if not is_release(page, target):
            return False
        journal.write("globe_found", ticker=target.ticker, number=page.number, title=page.title)
        journal.say(f"{target.ticker} globenewswire_page: NEW * {page.title}")
        if notify:
            sighting = Sighting(
                str(page.number), page.title, page.url, page.published, earnings=True
            )
            notify(source, sighting)
        return True

    try:
        for start, stop in spans(target.times):
            until = min(stop, end)
            if until <= journal.now():
                continue
            # Waits on the journal's trusted clock, checked every 30 s at most
            while (wait := (start - journal.now()).total_seconds()) > 0:  # noqa: ASYNC110
                await asyncio.sleep(min(wait, 30))
            if await frontier.watch(until, found) in {"found", "refused", "budget"}:
                return
    except Exception as error:  # a bug here mustn't stop the window
        journal.write("globe_guess_error", label=target.ticker, message=repr(error))


class GlobeBatch(BaseModel):
    """A record-only run of the guessing over one of GlobeNewswire's busy minutes, with no
    company: how many releases a batch has and how soon each page opens. A JSON file in the
    schedule folder's `trials/globe/`."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    starts_at: datetime
    duration_seconds: float = Field(default=360, ge=10, le=600)
    max_requests: int = Field(default=BUDGET, ge=1, le=BUDGET)

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(seconds=self.duration_seconds)


async def run_batch(batch: GlobeBatch, fetcher: Fetcher, journal: "Journal") -> None:
    """Records every page of a batch as it opens; posts nothing."""

    async def keep_going(page: Page) -> bool:
        return False

    try:
        while (wait := (batch.starts_at - journal.now()).total_seconds()) > 0:  # noqa: ASYNC110
            await asyncio.sleep(min(wait, 30))
        if journal.now() >= batch.ends_at:
            return
        frontier = Frontier(fetcher, journal, batch.id, budget=batch.max_requests)
        journal.write("globe_batch_started", batch=batch.id, ends_at=batch.ends_at)
        outcome = await frontier.watch(batch.ends_at, keep_going)
        journal.write(
            "globe_batch_finished", batch=batch.id, outcome=outcome, guesses=frontier.sent
        )
    except Exception as error:
        journal.write("globe_guess_error", label=batch.id, message=repr(error))
