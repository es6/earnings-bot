"""Latency recorder: poll every source around a scheduled release and log when each one first
shows it.

    uv run earnings-bot-record schedules/2026-11-02.toml

The plan file lists the releases to watch. Every event is appended to a JSON Lines log next to
the plan, and a summary prints when each watch window closes.
"""

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
import tomllib
from collections.abc import Callable, Hashable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from pydantic import BaseModel, ValidationError, field_validator

from earnings_bot.browser import BrowserError, BrowserUnavailable
from earnings_bot.browser_sources import IRTrial
from earnings_bot.clock import TrustedClock
from earnings_bot.config import RecorderSettings
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.sources import (
    BUSINESS_WIRE_EARNINGS,
    GLOBENEWSWIRE_EARNINGS,
    PR_NEWSWIRE_EARNINGS,
    PR_NEWSWIRE_SEMICONDUCTORS,
    BroadcomNewsroom,
    EdgarFilings,
    EdgarLatest,
    GuessedFile,
    GuessedPage,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
    published_time,
)

ET = ZoneInfo("America/New_York")

# Response headers worth keeping, to see how stale a CDN's copy was
CACHE_HEADERS = ("age", "cache-control", "expires", "last-modified", "date", "cf-cache-status")

# Seconds between clock syncs during a window. A computer's clock can drift by about a
# millisecond a minute, so this keeps the error to a few milliseconds.
CLOCK_RESYNC = 300


class Trial(BaseModel):
    """Sources on trial, a `[target.trial]` table in the plan file. They're polled and logged like
    any other source, but what they see only posts to Discord when `post` is true, once release
    days have shown they're right and fast."""

    post: bool = False
    broadcom_newsroom: bool = False  # Broadcom's newsroom JSON
    rss: list[str] = []  # company press release feeds, always cache-busted
    globenewswire: list[str] = []  # names to look for in GlobeNewswire's earnings feed
    # Names to look for in PR Newswire's Semiconductors feed
    prnewswire_semiconductors: list[str] = []
    pages: list[str] = []  # exact URLs of the release page or file, fetched whole
    ir: IRTrial | None = None  # issuer browser listings and official PDFs

    def sources(self) -> list[Source]:
        sources: list[Source] = []
        if self.broadcom_newsroom:
            sources.append(BroadcomNewsroom())
        sources += [_company_feed(url) for url in self.rss]
        if self.globenewswire:
            # Both copies, so a live release shows whether busting GlobeNewswire's cache helps
            sources += [
                RssFeed("globenewswire", GLOBENEWSWIRE_EARNINGS, self.globenewswire),
                RssFeed("globenewswire_busted", GLOBENEWSWIRE_EARNINGS, self.globenewswire, True),
            ]
        if self.prnewswire_semiconductors:
            sources.append(
                RssFeed(
                    "prnewswire_semiconductors",
                    PR_NEWSWIRE_SEMICONDUCTORS,
                    self.prnewswire_semiconductors,
                )
            )
        sources += _named_apart([GuessedPage(url) for url in self.pages])
        if self.ir:
            sources += self.ir.sources()
        for source in sources:
            source.trial = True
        return sources


def _company_feed(url: str) -> RssFeed:
    return RssFeed(f"rss:{urlsplit(url).hostname}", url, bust_cache=True)


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
    q4_host: str | None = None
    newswire: list[str] = []  # names to look for in Business Wire's earnings feed
    prnewswire: list[str] = []  # names to look for in PR Newswire's earnings feed
    rss: list[str] = []  # the company's own press release feeds
    guess: list[str] = []  # file URLs to probe
    trial: Trial = Trial()
    before_minutes: float = 10
    after_minutes: float = 30

    @field_validator("expected")
    @classmethod
    def _eastern(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=ET)

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
        sources += [EdgarFilings(self.cik), EdgarLatest(self.cik)]
        if self.newswire:
            # Only the cache-busted copy: on Nov 4 the plain one trailed it by 2 minutes both times
            sources.append(
                RssFeed("businesswire_busted", BUSINESS_WIRE_EARNINGS, self.newswire, True)
            )
        if self.prnewswire:
            sources.append(RssFeed("prnewswire", PR_NEWSWIRE_EARNINGS, self.prnewswire))
        sources += [_company_feed(url) for url in self.rss]
        sources += _named_apart([GuessedFile(url) for url in self.guess])
        return sources + self.trial.sources()


class Plan(BaseModel):
    target: list[Target]


# Called with each new item a source lists during a window
OnNew = Callable[[Target, Source, Sighting], None]


def load_plan(path: Path) -> Plan:
    with path.open("rb") as file:
        return Plan.model_validate(tomllib.load(file))


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
    stopped: str | None = None

    @property
    def done(self) -> bool:
        return self.stopped is not None or bool(self.source.stop_when_found and self.known)


class PollError(Exception):
    def __init__(
        self, message: str, *, pause: float = 0.0, fatal: bool = False, timeout: bool = False
    ) -> None:
        super().__init__(message)
        self.pause = pause
        self.fatal = fatal
        self.timeout = timeout


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
        raise PollError("HTTP 403, so the site doesn't want us here. Stopped.", fatal=True)
    if response.status in (429, 503):
        retry_after = response.headers.get("retry-after", "")
        pause = float(retry_after) if retry_after.isdigit() else 30.0
        raise PollError(f"HTTP {response.status}, backing off for {pause:.0f}s", pause=pause)
    if response.status not in source.ok_statuses:
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


def _caught_up(w: Watch, item: Sighting) -> bool:
    """Whether an item listed at the first poll was published inside the window. Only sources
    whose timestamps name their time zone can tell; Q4's site-local times can't. Nor can a
    guessed URL, whose Last-Modified can be when a copy was generated: a page that answers 200
    for anything would otherwise look like the release the moment the window opened."""
    published = published_time(item.published)
    if not item.earnings or w.window is None or published is None:
        return False
    if isinstance(w.source, GuessedFile):
        return False
    start, end = w.window
    return start <= published <= end


def _logged(item: Sighting) -> dict[str, Any]:
    # The page itself, when the poll downloaded one, is far too big for the log
    fields = asdict(replace(item, body=None))
    del fields["body"]
    return fields


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
    for item in items:  # a key listed twice counts once
        if item.key in known:
            continue
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
            headers={
                name: response.headers[name] for name in CACHE_HEADERS if name in response.headers
            },
        )
        if item.earnings and item.caught_up:
            w.caught_up = w.caught_up or item.title
        elif item.earnings and w.first is None:
            w.first, w.first_title = seen, item.title
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
        if on_new:
            on_new(w.source, item)


def _failed(w: Watch, error: PollError, journal: Journal) -> float:
    if error.timeout:
        w.timeouts += 1
    else:
        w.errors += 1
    timeout = {"timeout": True} if error.timeout else {}
    journal.write("error", ticker=w.ticker, source=w.source.name, message=str(error), **timeout)
    if str(error) != w.problem:
        journal.say(f"{w.ticker} {w.source.name}: {error}")
    w.problem = str(error)
    if error.fatal:
        w.stopped = str(error)
    return error.pause


def take(
    w: Watch,
    sent: datetime,
    outcome: Response | PollError,
    journal: Journal,
    on_new: Callable[[Source, Sighting], None] | None = None,
) -> float:
    """Records one poll's outcome for one watch, and returns how many seconds more than usual
    to wait before the next poll."""
    w.polls += 1
    if isinstance(outcome, PollError):
        return _failed(w, outcome, journal)
    try:
        items = parse(w.source, outcome)
    except PollError as error:
        return _failed(w, error, journal)
    if w.problem:
        journal.say(f"{w.ticker} {w.source.name}: working again")
        w.problem = None
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


async def watch(
    w: Watch,
    fetcher: Fetcher,
    journal: Journal,
    end: datetime,
    on_new: Callable[[Source, Sighting], None] | None = None,
) -> None:
    """Polls one watch's source on its own until `end`, or until the watch is done."""
    while (sent := journal.now()) < end:
        next_poll = time.monotonic() + w.source.interval
        pause = take(w, sent, await _poll_once(w.source, fetcher), journal, on_new)
        if w.done:
            return
        await asyncio.sleep(max(0.0, next_poll + pause - time.monotonic()))


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
            await member.left
        finally:
            self._leave(member)
            if not self.members and self.task is not None and not self.task.done():
                self.task.cancel()

    def _leave(self, member: _Member) -> None:
        if member in self.members:
            self.members.remove(member)
        if not member.left.done():
            member.left.set_result(None)

    async def _run(self) -> None:
        try:
            while True:
                sent = self.journal.now()
                for member in [m for m in self.members if sent >= m.end]:
                    self._leave(member)
                if not self.members:
                    return
                next_poll = time.monotonic() + self.source.interval
                outcome = await _poll_once(self.source, self.fetcher)
                pause = 0.0
                for member in list(self.members):
                    w = member.watch
                    pause = max(pause, take(w, sent, outcome, self.journal, member.on_new))
                    if w.done:
                        self._leave(member)
                await asyncio.sleep(max(0.0, next_poll + pause - time.monotonic()))
        except Exception as error:
            # Every watch fails the way it would have polling on its own
            for member in self.members:
                if not member.left.done():
                    member.left.set_exception(error)
            self.members.clear()


class Polls:
    """Runs every watch, sharing one poll loop among watches whose sources have the same
    `share_key` and leaving the rest to poll on their own."""

    def __init__(self, fetcher: Fetcher, journal: Journal) -> None:
        self.fetcher = fetcher
        self.journal = journal
        self.shared: dict[Hashable, SharedPoll] = {}

    async def watch(
        self,
        w: Watch,
        end: datetime,
        on_new: Callable[[Source, Sighting], None] | None = None,
    ) -> None:
        key = w.source.share_key
        if key is None:
            await watch(w, self.fetcher, self.journal, end, on_new)
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
        f"{target.ticker}: expected {_clock(target.expected)} ET. First seen is the first item"
        " that looks like the release. Queued is the median wait for the host's turn.",
        f"  {'source':<{width}}  {'first seen':<12}  {'vs expected':>11}  {'vs first':>9}"
        f"  {'polls':>5}  {'errors':>6}  {'timeouts':>8}  {'median':>7}  {'queued':>7}",
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
                "trial": w.source.trial,
                "first": w.first,
                "title": w.first_title,
                "caught_up": w.caught_up or None,
                "polls": w.polls,
                "errors": w.errors,
                "timeouts": w.timeouts,
                "stopped": w.stopped,
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
) -> None:
    start, end = target.window
    if journal.now() >= end:
        journal.say(f"{target.ticker}: its window closed at {_clock(end)} ET, skipping")
        return
    if journal.now() < start:
        journal.say(f"{target.ticker}: waiting until {_clock(start)} ET")
    # Wait on the system clock, which catches up after the computer sleeps, in short naps so a
    # sleep doesn't make us miss the window. The clock is synced again once the window opens.
    while (remaining := (start - datetime.now(UTC)).total_seconds()) > 0:  # noqa: ASYNC110
        await asyncio.sleep(min(remaining, 30))
    await sync_clock(journal)

    polls = polls or Polls(fetcher, journal)
    watches = [Watch(target.ticker, source, (start, end)) for source in target.sources()]
    notify = (lambda source, item: on_new(target, source, item)) if on_new else None
    journal.write(
        "window",
        ticker=target.ticker,
        expected=target.expected,
        start=start,
        end=end,
        sources=[w.source.name for w in watches],
        trial=[w.source.name for w in watches if w.source.trial],
    )
    journal.say(f"{target.ticker}: watching {len(watches)} sources until {_clock(end)} ET")
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(keep_clock_synced(journal, end))
            for w in watches:
                group.create_task(polls.watch(w, end, notify))
    finally:
        summarize(target, watches, journal)


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
