"""Latency recorder: poll every source around a scheduled release and log when each one first
shows it.

    uv run earnings-bot-record schedules/2026-11-02.toml

The plan file lists the releases to watch. Every event is appended to a JSON Lines log next to
the plan, and a summary prints when each watch window closes.
"""

import argparse
import asyncio
import contextlib
import io
import json
import statistics
import sys
import time
import tomllib
from collections.abc import Callable, Hashable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from earnings_bot.browser import BrowserError, BrowserUnavailable
from earnings_bot.browser_sources import BrowserRelease, BrowserRss, IRSource
from earnings_bot.clock import TrustedClock
from earnings_bot.config import RecorderSettings
from earnings_bot.fetch import Fetcher, Response, RoutePause
from earnings_bot.periods import agrees_with_period
from earnings_bot.sec_results import SixKResults, SixKUnavailable, is_results_release
from earnings_bot.sources import (
    BUSINESS_WIRE_EARNINGS,
    GLOBENEWSWIRE_EARNINGS,
    PR_NEWSWIRE_EARNINGS,
    PR_NEWSWIRE_SEMICONDUCTORS,
    BroadcomNewsroom,
    EdgarFilings,
    EdgarLatest,
    FreshPrNewswire,
    GuessedFile,
    GuessedPage,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    SecForm,
    Sighting,
    Source,
    needs_exhibit_check,
    published_time,
    results_headline,
)
from earnings_bot.text import html_to_text, verified_report

ET = ZoneInfo("America/New_York")

# Response headers worth keeping, to see how stale a CDN's copy was
CACHE_HEADERS = (
    "age",
    "cache-control",
    "expires",
    "last-modified",
    "date",
    "etag",
    "cf-cache-status",
    "x-cache",
    "x-cache-remote",
    "x-cache-hits",
    "x-cache-status",
    "x-varnish",
    "cache-status",
)

# Seconds between clock syncs during a window. A computer's clock can drift by about a
# millisecond a minute, so this keeps the error to a few milliseconds.
CLOCK_RESYNC = 300


class Trial(BaseModel):
    """More sources for a release, a `[target.trial]` table in the plan file. They post like any
    other source: a new source goes live, since a release day is the only way to time it. The
    table keeps its old name, and `post` is ignored, so older schedules still load."""

    post: bool = False  # ignored
    broadcom_newsroom: bool = False  # Broadcom's newsroom JSON
    rss: list[str] = []  # company press release feeds
    globenewswire: list[str] = []  # names to look for in GlobeNewswire's earnings feed
    # Names to look for in PR Newswire's Semiconductors feed
    prnewswire_semiconductors: list[str] = []
    pages: list[str] = []  # exact URLs of the release page or file, fetched whole

    def sources(
        self,
        rss_canonical: Sequence[str] = (),
        rss_spelled: Sequence[str] = (),
        *,
        publisher: str | None = None,
    ) -> list[Source]:
        sources: list[Source] = []
        if self.broadcom_newsroom:
            sources.append(BroadcomNewsroom())
        sources += [
            _company_feed(url, bust_cache=url not in rss_canonical, spell=url in rss_spelled)
            for url in self.rss
        ]
        if self.globenewswire:
            # Both copies, so a live release shows whether busting GlobeNewswire's cache helps
            sources += [
                RssFeed(
                    "globenewswire", GLOBENEWSWIRE_EARNINGS, self.globenewswire, publisher=publisher
                ),
                RssFeed(
                    "globenewswire_busted",
                    GLOBENEWSWIRE_EARNINGS,
                    self.globenewswire,
                    True,
                    publisher=publisher,
                ),
            ]
        if self.prnewswire_semiconductors:
            sources.append(
                FreshPrNewswire(
                    "prnewswire_semiconductors",
                    PR_NEWSWIRE_SEMICONDUCTORS,
                    self.prnewswire_semiconductors,
                    interval=RssFeed.interval,
                    publisher=publisher,
                )
            )
        sources += _named_apart([GuessedPage(url) for url in self.pages])
        return sources


def _company_feed(url: str, *, bust_cache: bool = True, spell: bool = False) -> RssFeed:
    # A spelled feed's cache ignores query strings, so it gets no query
    return RssFeed(
        f"rss:{urlsplit(url).hostname}", url, bust_cache=bust_cache and not spell, spell=spell
    )


def _named_apart[G: GuessedFile](guesses: Sequence[G]) -> list[G]:
    """Gives guesses that would share a name, such as two files of one name in different
    folders or two pages on one host, names that include their folder or host."""
    names = [guess.name for guess in guesses]
    for guess in guesses:
        if names.count(guess.name) > 1:
            kind = guess.name.split(":", 1)[0]
            url = urlsplit(guess.file_url)
            parts = [url.hostname or "", *(part for part in url.path.split("/") if part)]
            guess.name = f"{kind}:" + "/".join(parts[-2:])
    return list(guesses)


class Target(BaseModel):
    """One release to watch, as written in the plan file."""

    ticker: str
    expected: datetime  # US Eastern unless it carries a UTC offset
    cik: int
    sec_form: SecForm = "8-K"
    period: str | None = Field(default=None, pattern=r"^FY\d{4}Q[1-4]$")
    q4_host: str | None = None
    newswire: list[str] = []  # names to look for in Business Wire's earnings feed
    prnewswire: list[str] = []  # names to look for in PR Newswire's earnings feed
    wire_publisher: str | None = None  # strict match when a brand is one word
    rss: list[str] = []  # the company's own press release feeds
    rss_canonical: list[str] = []  # issuer feeds that reject the query parameter
    # Issuer feeds whose cache ignores query strings but misses on a new spelling of the path
    rss_spelled: list[str] = []
    guess: list[str] = []  # file URLs to probe
    ir: IRSource | None = None
    trial: Trial = Trial()
    before_minutes: float = 10
    after_minutes: float = 30

    @model_validator(mode="before")
    @classmethod
    def _migrate_ir(cls, values: Any) -> Any:
        # Schedule TOML and saved calendar JSON used to nest IR under trial.
        if (
            isinstance(values, dict)
            and isinstance(trial := values.get("trial"), dict)
            and "ir" in trial
        ):
            return {
                "ir": trial["ir"],
                **values,
                "trial": {k: v for k, v in trial.items() if k != "ir"},
            }
        return values

    @field_validator("expected")
    @classmethod
    def _eastern(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=ET)

    @property
    def release_key(self) -> str:
        return f"{self.cik}:{self.period or self.expected.astimezone(ET).date()}"

    @property
    def window(self) -> tuple[datetime, datetime]:
        return (
            self.expected - timedelta(minutes=self.before_minutes),
            self.expected + timedelta(minutes=self.after_minutes),
        )

    def sources(self) -> list[Source]:
        sources: list[Source] = []
        if self.q4_host:
            sources += [Q4PressReleases(self.q4_host), Q4FinancialReports(self.q4_host)]
        sources += [
            EdgarFilings(self.cik, form=self.sec_form),
            EdgarLatest(self.cik, form=self.sec_form),
        ]
        if self.newswire:
            # Only the cache-busted copy: on Nov 4 the plain one trailed it by 2 minutes both times
            sources.append(
                RssFeed(
                    "businesswire_busted",
                    BUSINESS_WIRE_EARNINGS,
                    self.newswire,
                    True,
                    publisher=self.wire_publisher,
                )
            )
        if self.prnewswire:
            # The fresh copy goes first. The cached one stays in case PR Newswire's own server
            # stops answering.
            sources += [
                FreshPrNewswire(
                    "prnewswire_busted",
                    PR_NEWSWIRE_EARNINGS,
                    self.prnewswire,
                    publisher=self.wire_publisher,
                ),
                RssFeed(
                    "prnewswire",
                    PR_NEWSWIRE_EARNINGS,
                    self.prnewswire,
                    publisher=self.wire_publisher,
                ),
            ]
        sources += [
            _company_feed(
                url, bust_cache=url not in self.rss_canonical, spell=url in self.rss_spelled
            )
            for url in self.rss
        ]
        sources += _named_apart([GuessedFile(url) for url in self.guess])
        if self.ir:
            sources += self.ir.sources(self.expected.astimezone(ET).date(), self.period)
        return sources + self.trial.sources(
            self.rss_canonical, self.rss_spelled, publisher=self.wire_publisher
        )


class Plan(BaseModel):
    target: list[Target]


# Called with each new item a source lists during a window
OnNew = Callable[[Target, Source, Sighting], None]


def parse_plan(data: bytes) -> Plan:
    # utf-8-sig, so a schedule saved by Notepad with a byte order mark still reads
    return Plan.model_validate(tomllib.loads(data.decode("utf-8-sig")))


def load_plan(path: Path) -> Plan:
    return parse_plan(path.read_bytes())


def _clock(moment: datetime) -> str:
    return f"{moment.astimezone(ET):%H:%M:%S.%f}"[:-3]


def _json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    return str(value)


class Journal:
    """Appends every event to the JSON Lines log, and prints the ones worth watching live."""

    def __init__(self, file: TextIO, clock: TrustedClock | None = None) -> None:
        self.file = file
        self.clock = clock or TrustedClock()

    def now(self) -> datetime:
        return self.clock.now()

    def write(self, event: str, **fields: Any) -> None:
        record = {"at": self.now(), "event": event, **fields}
        self.file.write(json.dumps(record, default=_json_value) + "\n")
        self.file.flush()

    def say(self, message: str) -> None:
        print(f"{_clock(self.now())}  {message}", flush=True)


def configure_console() -> None:
    """Makes printing to the console unable to fail. When output goes to a file or pipe, as
    scripts/run-bot.ps1 sends it, Windows Python encodes it as cp1252 and raises on anything
    outside it. A headline with a zero-width space would then crash the poll that found it."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


@dataclass(eq=False)
class Watch:
    """What one source has shown for one target so far."""

    ticker: str
    source: Source
    # The target's window. An earnings item already listed at the first poll still counts as new
    # if its own timestamp falls inside it, so starting late doesn't lose the release.
    window: tuple[datetime, datetime] | None = None
    polls: int = 0
    errors: int = 0  # failed polls, apart from timeouts
    timeouts: int = 0  # polls that got no answer within the source's timeout
    elapsed: list[float] = field(default_factory=list)
    queued: list[float] = field(default_factory=list)  # seconds each poll waited for its host
    known: set[str] | None = None  # keys of the items listed so far; None before the first poll
    first: datetime | None = None  # when the first new earnings item showed up
    first_title: str = ""
    # The first earnings item that was already available at the first poll and matches this
    # window or scheduled release. It doesn't say how fast the source is, so it never sets `first`.
    caught_up: str = ""
    previous: datetime | None = None  # when the last answered poll went out
    problem: str | None = None  # the current error, so a failing source reports it only once
    # When the route may be asked again after a 403 or a Retry-After. watch() swaps in the one
    # the fetcher shares among loops reading the same route (Source.pause_route).
    pause: RoutePause = field(default_factory=RoutePause)
    blocked: int = 0  # polls the site refused in all
    reported: float | None = None  # time.monotonic() of the last progress event
    stopped: str | None = None
    period: str | None = None

    @property
    def done(self) -> bool:
        return self.stopped is not None or bool(self.source.stop_when_found and self.known)

    @property
    def blocks(self) -> int:
        """403s in a row, which lengthen the pause before the next try."""
        return self.pause.blocks


class PollError(Exception):
    def __init__(
        self,
        message: str,
        *,
        pause: float = 0.0,
        fatal: bool = False,
        timeout: bool = False,
        blocked: bool = False,
    ) -> None:
        super().__init__(message)
        self.pause = pause
        self.fatal = fatal
        self.timeout = timeout
        self.blocked = blocked


async def fetch(source: Source, fetcher: Fetcher) -> Response:
    """One poll's request. Raises PollError when the answer can't be used."""
    try:
        response = await source.fetch(fetcher)
    except BrowserUnavailable as error:
        raise PollError(f"desktop Chrome unavailable: {error}", fatal=True) from error
    except BrowserError as error:
        raise PollError(f"desktop Chrome: {error}") from error
    except TimeoutError as error:
        raise PollError(f"no answer within {source.timeout:g}s", timeout=True) from error
    except aiohttp.ClientError as error:
        raise PollError(f"{type(error).__name__} {error}".strip()) from error
    if response.status == 403:
        raise PollError("HTTP 403: the site refused us. Trying again after a pause", blocked=True)
    if response.status in (429, 503):
        retry_after = response.headers.get("retry-after", "")
        pause = float(retry_after) if retry_after.isdigit() else 30.0
        raise PollError(f"HTTP {response.status}, backing off for {pause:.0f}s", pause=pause)
    if response.status not in source.ok_statuses:
        if response.status == 0:
            raise PollError("Chrome stopped at a redirect")
        raise PollError(f"HTTP {response.status}")
    return response


def parse(source: Source, response: Response) -> list[Sighting]:
    try:
        return source.parse(response)
    except (ValueError, KeyError, TypeError) as error:
        raise PollError(f"unexpected response: {error!r}") from error


async def poll(source: Source, fetcher: Fetcher) -> tuple[Response, list[Sighting]]:
    response = await fetch(source, fetcher)
    return response, parse(source, response)


# How long before a window opens a release can come out and still be this one. On published
# its Q3 2026 results at 5:00 AM ET, ten minutes before the watch its 6-K times had set.
EARLY_RELEASE = timedelta(hours=12)


def _site_local_day(stamp: str | None) -> date | None:
    """The day of Q4's "11/10/2026 05:00:00", which names no time zone."""
    try:
        return datetime.strptime(stamp or "", "%m/%d/%Y %H:%M:%S").date()
    except ValueError:
        return None


def _caught_up(w: Watch, item: Sighting) -> bool:
    """Whether an earnings item listed at the first poll is this release, already out: published
    inside the window, or earlier that day, up to 12 hours before it opened. A Q4 time names no
    zone, so its day decides. A guessed URL can't tell, since its Last-Modified can be when a copy
    was generated: a page that answers 200 for anything would otherwise look like the release."""
    if not item.earnings or w.window is None or isinstance(w.source, GuessedFile):
        return False
    start, end = w.window
    day = start.astimezone(ET).date()
    if (published := published_time(item.published)) is not None:
        return start <= published <= end or (
            start - EARLY_RELEASE <= published < start and published.astimezone(ET).date() == day
        )
    return _site_local_day(item.published) == day


def _logged(item: Sighting) -> dict[str, Any]:
    # The page itself, when the poll downloaded one, is far too big for the log
    fields = asdict(replace(item, body=None))
    del fields["body"]
    del fields["lead"]
    return fields


def _verified_item(w: Watch, item: Sighting) -> bool:
    """Validate the period and distinguish a report from other news mentioning a quarter."""
    if not agrees_with_period(item.details.get("results_headline", item.title), w.period):
        return False
    if isinstance(w.source, RssFeed | BroadcomNewsroom | Q4PressReleases):
        return results_headline(item.title) or (
            bool(item.lead)
            and results_headline(item.lead)
            and agrees_with_period(item.lead, w.period)
        )
    if isinstance(w.source, GuessedPage):
        if item.body is None:
            return False
        if item.body.startswith(b"%PDF-"):
            return True  # exact, preconfigured file URL
        text = html_to_text(item.body.decode(errors="replace"))
        return (results_headline(item.title) and verified_report(text)) or is_results_release(
            text, ""
        )
    if isinstance(w.source, Q4FinancialReports):
        label = item.title.split(": ", 1)[-1].casefold()
        return any(
            word in label
            for word in (
                "release",
                "financial statements",
                "earnings presentation",
                "results snapshot",
                "shareholder letter",
            )
        )
    return True


def _cache_headers(response: Response) -> dict[str, str]:
    return {name: response.headers[name] for name in CACHE_HEADERS if name in response.headers}


def record_items(
    w: Watch,
    items: list[Sighting],
    response: Response,
    journal: Journal,
    sent: datetime,
    on_new: Callable[[Source, Sighting], None] | None = None,
) -> None:
    first_poll = w.known is None
    known = w.known if w.known is not None else set[str]()
    new: list[Sighting] = []
    for parsed in items:  # a key listed twice counts once
        item = parsed
        if item.key in known:
            continue
        if item.earnings and not _verified_item(w, item):
            if w.source.stop_when_found:
                continue  # An incomplete page at a guessed URL can still become the report.
            item = replace(item, earnings=False)
        known.add(item.key)
        if not first_poll:
            new.append(item)
        elif w.source.report_initial or _caught_up(w, item):
            new.append(replace(item, caught_up=True))
    if first_poll:
        w.known = known
        journal.write(
            "ready", ticker=w.ticker, source=w.source.name, listed=len(items), caught_up=len(new)
        )
        late = f", {len(new)} already out" if new else ""
        journal.say(f"{w.ticker} {w.source.name}: ready, {len(items)} listed{late}")
    for item in new:
        seen = journal.now()
        # The item appeared between the previous poll going out and this response arriving
        journal.write(
            "new",
            ticker=w.ticker,
            source=w.source.name,
            **_logged(item),
            seen=seen,
            sent=sent,
            previous_sent=w.previous,
            response_ms=round(response.elapsed * 1000),
            queued_ms=round(response.queued * 1000),
            request_url=response.url,
            headers=_cache_headers(response),
        )
        if item.earnings and item.caught_up:
            w.caught_up = w.caught_up or item.title
        elif item.earnings and w.first is None:
            w.first, w.first_title = seen, item.title
        if on_new:
            # Before anything else can go wrong with this item. The poster only schedules its
            # work, and a bug in it mustn't stop this source, or a shared poll's other
            # companies, from polling.
            try:
                on_new(w.source, item)
            except Exception as error:
                journal.write(
                    "post_error", ticker=w.ticker, source=w.source.name, message=repr(error)
                )
                journal.say(f"{w.ticker} {w.source.name}: couldn't post {item.key}: {error!r}")
        late = " (published before we started)" if item.caught_up else ""
        star = " *" if item.earnings else ""
        journal.say(f"{w.ticker} {w.source.name}: NEW{star} {item.title}{late}")
        if item.body is not None:
            # The poll downloaded the release itself, so its text is here already
            journal.write(
                "body",
                ticker=w.ticker,
                source=w.source.name,
                key=item.key,
                via="poll",
                bytes=len(item.body),
            )


def _failed(w: Watch, error: PollError, journal: Journal) -> float:
    if error.timeout:
        w.timeouts += 1
    else:
        w.errors += 1
    extra: dict[str, Any] = {"timeout": True} if error.timeout else {}
    pause = error.pause
    if error.blocked:
        w.blocked += 1
        pause = w.pause.refused()
        extra["retry_in"] = pause
    elif pause:
        w.pause.hold(pause)
    if http_failure := getattr(w.source, "http_failure", None):
        extra["http_failure"] = http_failure  # why the direct request gave way to Chrome
    journal.write("error", ticker=w.ticker, source=w.source.name, message=str(error), **extra)
    if str(error) != w.problem:
        journal.say(f"{w.ticker} {w.source.name}: {error}")
    w.problem = str(error)
    if error.fatal:
        w.stopped = str(error)
    return pause


# How often each watch writes a `progress` event, for the watchdog (earnings_bot.watchdog)
PROGRESS_SECONDS = 30.0


def _progress(w: Watch, journal: Journal, *, now: bool = False) -> None:
    """Says that the watch is polling and when its source last answered, every PROGRESS_SECONDS
    and at once when it starts failing, recovers or finishes. Most polls that find nothing log
    nothing, so without this a stalled or refused route looks the same as a quiet one."""
    if not now and w.reported is not None and time.monotonic() - w.reported < PROGRESS_SECONDS:
        return
    w.reported = time.monotonic()
    journal.write(
        "progress",
        ticker=w.ticker,
        source=w.source.name,
        interval=w.source.interval,
        polls=w.polls,
        answered=w.previous,  # when the last answered poll went out
        problem=w.problem,
        blocked=w.blocked,
        retry_in=round(w.pause.left(), 1),
        done=w.done,
    )


def take(
    w: Watch,
    sent: datetime,
    outcome: Response | PollError,
    journal: Journal,
    on_new: Callable[[Source, Sighting], None] | None = None,
    *,
    items: list[Sighting] | None = None,
) -> float:
    """Records one poll's outcome for one watch, and returns how many seconds more than usual
    to wait before the next poll."""
    problem = w.problem
    pause = _take(w, sent, outcome, journal, on_new, items=items)
    _progress(w, journal, now=w.done or w.problem != problem)
    return pause


def _take(
    w: Watch,
    sent: datetime,
    outcome: Response | PollError,
    journal: Journal,
    on_new: Callable[[Source, Sighting], None] | None = None,
    *,
    items: list[Sighting] | None = None,
) -> float:
    w.polls += 1
    if isinstance(w.source, BrowserRelease) and w.source.listing_poll:
        listing, received = w.source.listing_poll
        journal.write(
            "issuer_table_listing"
            if w.source.name == "issuer-table"
            else "http_ir_listing"
            if w.source.name == "http-ir"
            else "browser_listing",
            ticker=w.ticker,
            source=w.source.name,
            transport=w.source.listing_transport,
            sent=sent,
            received=journal.now() - timedelta(seconds=time.monotonic() - received),
            matched=w.source.matched_listing,
            status=listing.status,
            response_ms=round(listing.elapsed * 1000),
            queued_ms=round(listing.queued * 1000),
            headers=_cache_headers(listing),
            **(
                {"http_failure": failure}
                if (failure := getattr(w.source, "http_failure", None))
                else {}
            ),
        )
    if isinstance(outcome, PollError):
        return _failed(w, outcome, journal)
    if items is None:
        try:
            items = parse(w.source, outcome)
        except PollError as error:
            return _failed(w, error, journal)
    if isinstance(w.source, BrowserRss):
        if w.window is not None:
            items = [item for item in items if _caught_up(w, item)]
        journal.write(
            "browser_feed",
            ticker=w.ticker,
            source=w.source.name,
            sent=sent,
            matched=bool(items),
            response_ms=round(outcome.elapsed * 1000),
            queued_ms=round(outcome.queued * 1000),
            headers=_cache_headers(outcome),
        )
    if w.problem:
        journal.say(f"{w.ticker} {w.source.name}: working again")
        w.problem = None
    w.pause.answered()
    w.elapsed.append(outcome.elapsed)
    w.queued.append(outcome.queued)
    record_items(w, items, outcome, journal, sent, on_new)
    w.previous = sent
    return 0.0


async def _poll_once(source: Source, fetcher: Fetcher) -> Response | PollError:
    try:
        return await fetch(source, fetcher)
    except PollError as error:
        return error


async def _verify_sixks(
    w: Watch, response: Response, sixk: SixKResults, journal: Journal
) -> list[Sighting]:
    items = []
    for item in parse(w.source, response):
        result = item
        accepted = published_time(item.published)
        if (
            needs_exhibit_check(item.details)
            and w.window
            and accepted is not None
            and _caught_up(w, replace(item, earnings=True))
            and item.key not in (w.known or ())
        ):
            try:
                result = await sixk.verify(item) or item
            except SixKUnavailable as error:
                # Leave the accession unseen so either SEC source can retry it.
                journal.write("sec_verify_error", ticker=w.ticker, key=item.key, message=str(error))
                continue
        items.append(result)
    return items


async def watch(
    w: Watch,
    fetcher: Fetcher,
    journal: Journal,
    end: datetime,
    on_new: Callable[[Source, Sighting], None] | None = None,
    *,
    sixk: SixKResults | None = None,
    interval: float | None = None,
) -> None:
    """Polls one watch's source on its own until `end`, or until the watch is done, every
    `interval` seconds instead of the source's own when given."""
    if (route := w.source.pause_route) is not None:
        w.pause = fetcher.route_pause(route)
    while (sent := journal.now()) < end:
        if (wait := w.pause.left()) > 0:
            # Another loop reading this route was refused or asked to wait
            await asyncio.sleep(min(wait, (end - sent).total_seconds()))
            continue
        next_poll = time.monotonic() + (interval or w.source.interval)
        outcome = await _poll_once(w.source, fetcher)
        items = None
        if sixk and isinstance(outcome, Response):
            try:
                items = await _verify_sixks(w, outcome, sixk, journal)
            except PollError as error:
                outcome = error
        pause = take(w, sent, outcome, journal, on_new, items=items)
        if w.done:
            return
        # A long Retry-After mustn't hold the window open past its end
        left = (end - journal.now()).total_seconds()
        await asyncio.sleep(max(0.0, min(next_poll + pause - time.monotonic(), left)))


@dataclass(eq=False)
class _Member:
    watch: Watch
    end: datetime
    on_new: Callable[[Source, Sighting], None] | None
    left: asyncio.Future[None]


class SharedPoll:
    """One poll loop for every watch whose source has the same `share_key`, such as one newswire
    feed watched for several companies at once. Each response goes to every watch, which reads
    it with its own source, so each still keeps only its own company's items."""

    def __init__(self, source: Source, fetcher: Fetcher, journal: Journal) -> None:
        self.source = source  # the first watch's, for the URL, interval and timeout
        self.fetcher = fetcher
        self.journal = journal
        self.members: list[_Member] = []
        self._next_poll = 0.0  # Keep backoff when the last watcher leaves.
        self.task: asyncio.Task[None] | None = None

    async def join(
        self,
        w: Watch,
        end: datetime,
        on_new: Callable[[Source, Sighting], None] | None = None,
    ) -> None:
        """Returns once the watch's window closes or the watch is done."""
        member = _Member(w, end, on_new, asyncio.get_running_loop().create_future())
        self.members.append(member)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())
        try:
            # Each member owns its deadline, even if it joined during an in-flight poll or backoff.
            while (remaining := (end - self.journal.now()).total_seconds()) > 0:
                done, _ = await asyncio.wait((member.left,), timeout=remaining)
                if done:
                    member.left.result()
                    return
        finally:
            self._leave(member)
            if not self.members and self.task is not None:
                self.task.cancel()
                self.task = None

    def _leave(self, member: _Member) -> None:
        if member in self.members:
            self.members.remove(member)
        if not member.left.done():
            member.left.set_result(None)

    async def _run(self) -> None:
        try:
            while self.members:
                if (wait := self._next_poll - time.monotonic()) > 0:
                    await asyncio.sleep(wait)
                    continue
                sent = self.journal.now()
                for member in [m for m in self.members if sent >= m.end]:
                    self._leave(member)
                if not self.members:
                    return
                self._next_poll = time.monotonic() + self.source.interval
                outcome = await _poll_once(self.source, self.fetcher)
                pause = 0.0
                for member in list(self.members):
                    w = member.watch
                    pause = max(pause, take(w, sent, outcome, self.journal, member.on_new))
                    if w.done:
                        self._leave(member)
                if pause:
                    self._next_poll = max(self._next_poll, time.monotonic()) + pause
        except Exception as error:
            # Every watch fails the way it would have polling on its own
            for member in self.members:
                if not member.left.done():
                    member.left.set_exception(error)
            self.members.clear()


class Polls:
    """Runs every watch, sharing one poll loop among watches whose sources have the same
    `share_key` and leaving the rest to poll on their own."""

    def __init__(self, fetcher: Fetcher, journal: Journal, sixk: SixKResults | None = None) -> None:
        self.fetcher = fetcher
        self.journal = journal
        self.sixk = sixk or SixKResults(fetcher)
        self.shared: dict[Hashable, SharedPoll] = {}

    async def watch(
        self,
        w: Watch,
        end: datetime,
        on_new: Callable[[Source, Sighting], None] | None = None,
    ) -> None:
        key = w.source.share_key
        if key is None:
            # 6-Ks and 8-Ks under Other Events are results only if their exhibit is
            sixk = self.sixk if isinstance(w.source, EdgarFilings) else None
            await watch(w, self.fetcher, self.journal, end, on_new, sixk=sixk)
            return
        if key not in self.shared:
            self.shared[key] = SharedPoll(w.source, self.fetcher, self.journal)
        await self.shared[key].join(w, end, on_new)


def _median_ms(values: list[float]) -> int | None:
    return round(statistics.median(values) * 1000) if values else None


def summarize(target: Target, watches: list[Watch], journal: Journal) -> None:
    def signed(seconds: float) -> str:
        return f"{seconds:+.1f}s"

    rows = sorted(watches, key=lambda w: w.first or datetime.max.replace(tzinfo=UTC))
    winner = rows[0].first if rows else None
    width = max(len(w.source.name) for w in watches)
    lines = [
        (
            f"{target.ticker}: expected {_clock(target.expected)} ET. First seen is the first"
            " item that looks like the release. Queued is the median wait for the host's turn."
        ),
        (
            f"  {'source':<{width}}  {'first seen':<12}  {'vs expected':>11}  {'vs first':>9}"
            f"  {'polls':>5}  {'errors':>6}  {'timeouts':>8}  {'median':>7}  {'queued':>7}"
        ),
    ]
    for w in rows:
        median = f"{statistics.median(w.elapsed) * 1000:.0f} ms" if w.elapsed else "-"
        queued = f"{statistics.median(w.queued) * 1000:.0f} ms" if w.queued else "-"
        if w.first and winner:
            seen = _clock(w.first)
            late = signed((w.first - target.expected).total_seconds())
            behind = signed((w.first - winner).total_seconds())
        else:
            seen = "stopped" if w.stopped else "caught up" if w.caught_up else "not seen"
            late, behind = "", ""
        lines.append(
            f"  {w.source.name:<{width}}  {seen:<12}  {late:>11}  {behind:>9}"
            f"  {w.polls:>5}  {w.errors:>6}  {w.timeouts:>8}  {median:>7}  {queued:>7}"
        )
    print("\n" + "\n".join(lines) + "\n", flush=True)
    journal.write(
        "summary",
        ticker=target.ticker,
        sources=[
            {
                "source": w.source.name,
                "first": w.first,
                "title": w.first_title,
                "caught_up": w.caught_up or None,
                "polls": w.polls,
                "errors": w.errors,
                "timeouts": w.timeouts,
                "stopped": w.stopped,
                "blocked": w.blocked,
                "median_ms": _median_ms(w.elapsed),
                "queued_median_ms": _median_ms(w.queued),
                "queued_max_ms": round(max(w.queued) * 1000) if w.queued else None,
            }
            for w in watches
        ],
    )


async def sync_clock(journal: Journal, periodic: bool = False) -> bool:
    """Syncs the journal's clock and logs the result. A periodic sync is quiet, and keeps the
    previous sync if no server answers."""
    # Windows that open together share a sync, since NIST asks for at most one query every
    # 4 seconds. This age is on the system clock because time.monotonic() can stop during sleep.
    if journal.clock.synced and time.time() - journal.clock.synced_at < 60:
        return True
    samples = await asyncio.to_thread(journal.clock.sync)
    if not samples:
        journal.write("clock", synced=False)
        if not periodic:
            # The last sync may be days old, and time.monotonic() can stop while the computer
            # sleeps, so the system clock is the safer guess
            journal.clock.forget()
            journal.say("Couldn't reach a time server, so times come from this PC's clock")
        return False
    best, behind = samples[0], journal.clock.system_clock_behind()
    journal.write(
        "clock",
        synced=True,
        server=best.server,
        max_error_ms=round(best.round_trip * 500, 1),
        system_clock_behind=round(behind, 4),
        # How far each server's answer was from the one in use, in milliseconds
        servers={s.server: round((s.offset - best.offset) * 1000, 1) for s in samples},
    )
    if not periodic:
        direction = "behind" if behind > 0 else "ahead"
        journal.say(
            f"Time is from {best.server}, accurate to {best.round_trip * 500:.0f} ms. "
            f"This PC's clock is {abs(behind):.3f}s {direction}."
        )
    return True


async def keep_clock_synced(journal: Journal, end: datetime) -> None:
    while (end - journal.now()).total_seconds() > CLOCK_RESYNC:
        await asyncio.sleep(CLOCK_RESYNC)
        await sync_clock(journal, periodic=True)


async def record_target(
    target: Target,
    fetcher: Fetcher,
    journal: Journal,
    on_new: OnNew | None = None,
    polls: Polls | None = None,
) -> list[Watch]:
    start, end = target.window
    if journal.now() >= end:
        journal.say(f"{target.ticker}: its window closed at {_clock(end)} ET, skipping")
        return []
    if journal.now() < start:
        # Name the day unless it's today: "waiting until 05:30" once read as this morning's
        opens = start.astimezone(ET)
        day = "" if opens.date() == journal.now().astimezone(ET).date() else f"{opens:%a %b %d} "
        journal.say(f"{target.ticker}: waiting until {day}{_clock(start)} ET")
    # Wait on the system clock, which catches up after the computer sleeps, in short naps so a
    # sleep doesn't make us miss the window. The clock is synced again once the window opens.
    while (remaining := (start - datetime.now(UTC)).total_seconds()) > 0:  # noqa: ASYNC110
        await asyncio.sleep(min(remaining, 30))
    await sync_clock(journal)

    polls = polls or Polls(fetcher, journal)
    watches = [
        Watch(target.ticker, source, (start, end), period=target.period)
        for source in target.sources()
    ]
    notify = (lambda source, item: on_new(target, source, item)) if on_new else None
    journal.write(
        "window",
        ticker=target.ticker,
        expected=target.expected,
        start=start,
        end=end,
        sources=[w.source.name for w in watches],
    )
    journal.say(f"{target.ticker}: watching {len(watches)} sources until {_clock(end)} ET")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(keep_clock_synced(journal, end))
            for w in watches:
                group.create_task(polls.watch(w, end, notify))
    finally:
        summarize(target, watches, journal)
    return watches


FOLLOW_UP = timedelta(hours=10)
FOLLOW_UP_INTERVAL = 60.0  # seconds


def followup_end(target: Target, longest: timedelta) -> datetime:
    end = target.window[1]
    day_end = end.astimezone(ET).replace(hour=23, minute=59, second=0, microsecond=0)
    return min(end + longest, day_end)


async def follow_up(
    target: Target,
    watches: list[Watch],
    fetcher: Fetcher,
    journal: Journal,
    on_new: OnNew | None = None,
    sixk: SixKResults | None = None,
    longest: timedelta = FOLLOW_UP,
) -> None:
    """After a window that found the release, check each source that didn't show it once a
    minute, until ten hours later or the end of that day, so links that come later still join
    the alert: Sea's 6-K comes hours after its Business Wire release, and its results page and
    newsroom later still. SEC's latest-filings feed scrolls too fast to read once a minute, so
    the company's own filing list stands in for it."""
    start, _ = target.window
    until = followup_end(target, longest)
    pending = [
        w
        for w in watches
        if w.first is None
        and not w.caught_up
        and w.stopped is None
        and w.source.name != "edgar_latest"
    ]
    if not pending or journal.now() >= until:
        return
    names = ", ".join(w.source.name for w in pending)
    journal.say(f"{target.ticker}: checking {names} for later links until {_clock(until)} ET")
    notify = (lambda source, item: on_new(target, source, item)) if on_new else None
    async with asyncio.TaskGroup() as group:
        for w in pending:
            w.window = (start, until)
            if w.known is None and isinstance(w.source, GuessedPage):
                # A full report at this configured URL can enrich an already delivered alert.
                # A HEAD-only guess still needs a baseline.
                w.known = set()
            filings = isinstance(w.source, EdgarFilings)
            group.create_task(
                watch(
                    w,
                    fetcher,
                    journal,
                    until,
                    notify,
                    sixk=sixk if filings else None,
                    interval=FOLLOW_UP_INTERVAL,
                )
            )


async def record(
    plan: Plan, fetcher: Fetcher, journal: Journal, on_new: OnNew | None = None
) -> None:
    journal.write("start", targets=[t.ticker for t in plan.target])
    await sync_clock(journal)
    # Overlapping windows that read the same feed share one poll of it
    polls = Polls(fetcher, journal)
    async with asyncio.TaskGroup() as group:
        for target in plan.target:
            group.create_task(record_target(target, fetcher, journal, on_new, polls))


async def _record_to(plan: Plan, sec_user_agent: str, log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as file:
        journal = Journal(file)
        journal.say(f"Logging to {log_path}. Press Ctrl+C to stop.")
        async with Fetcher(sec_user_agent) as fetcher:
            await record(plan, fetcher, journal)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="earnings-bot-record",
        description="Record when each source first shows a scheduled earnings release.",
    )
    parser.add_argument("plan", type=Path, help="TOML file listing the releases to watch")
    parser.add_argument("--log", type=Path, help="where to append events (default: PLAN.jsonl)")
    args = parser.parse_args()
    configure_console()

    try:
        # Pyright can't see that BaseSettings reads its fields from the environment
        settings = RecorderSettings()  # pyright: ignore[reportCallIssue]
    except ValidationError:
        sys.exit(
            "Set SEC_USER_AGENT in .env to your name and contact email, which SEC requires:\n"
            "  SEC_USER_AGENT=Jane Doe jane@example.com"
        )
    try:
        plan = load_plan(args.plan)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        sys.exit(f"Can't read {args.plan}: {error}")

    # Ctrl+C cancels the watches, which still print their summaries
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            _record_to(plan, settings.sec_user_agent, args.log or args.plan.with_suffix(".jsonl"))
        )


if __name__ == "__main__":
    main()
