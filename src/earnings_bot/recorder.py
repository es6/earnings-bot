"""Latency recorder: poll every source around a scheduled release and log when each one first
shows it.

    uv run earnings-bot-record recordings/2026-11-04.toml

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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from pydantic import BaseModel, ValidationError, field_validator

from earnings_bot.clock import TrustedClock
from earnings_bot.config import RecorderSettings
from earnings_bot.fetch import Fetcher, Response
from earnings_bot.sources import (
    BUSINESS_WIRE_EARNINGS,
    EdgarFilings,
    GuessedFile,
    Q4FinancialReports,
    Q4PressReleases,
    RssFeed,
    Sighting,
    Source,
)

ET = ZoneInfo("America/New_York")

# Response headers worth keeping, to see how stale a CDN's copy was
CACHE_HEADERS = ("age", "cache-control", "expires", "last-modified", "date", "cf-cache-status")

# Seconds between clock syncs during a window. A computer's clock can drift by about a
# millisecond a minute, so this keeps the error to a few milliseconds.
CLOCK_RESYNC = 300


class Target(BaseModel):
    """One release to watch, as written in the plan file."""

    ticker: str
    expected: datetime  # US Eastern unless it carries a UTC offset
    cik: int
    q4_host: str | None = None
    newswire: list[str] = []  # names to look for in Business Wire's earnings feed
    rss: list[str] = []  # the company's own press release feeds
    guess: list[str] = []  # file URLs to probe
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
        sources.append(EdgarFilings(self.cik))
        if self.newswire:
            # Polled both as is and with a cache-buster, to learn whether the feed's CDN delays it
            sources += [
                RssFeed("businesswire", BUSINESS_WIRE_EARNINGS, self.newswire),
                RssFeed("businesswire_busted", BUSINESS_WIRE_EARNINGS, self.newswire, True),
            ]
        sources += [RssFeed(f"rss:{urlsplit(url).hostname}", url) for url in self.rss]
        sources += [GuessedFile(url) for url in self.guess]
        return sources


class Plan(BaseModel):
    target: list[Target]


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


@dataclass
class Watch:
    """What one source has shown for one target so far."""

    ticker: str
    source: Source
    polls: int = 0
    errors: int = 0
    elapsed: list[float] = field(default_factory=list)
    known: set[str] | None = None  # keys of the items listed so far; None before the first poll
    first: datetime | None = None  # when the first new earnings item showed up
    first_title: str = ""
    problem: str | None = None  # the current error, so a failing source reports it only once
    stopped: str | None = None


class PollError(Exception):
    def __init__(self, message: str, *, pause: float = 0.0, fatal: bool = False) -> None:
        super().__init__(message)
        self.pause = pause
        self.fatal = fatal


async def poll(source: Source, fetcher: Fetcher) -> tuple[Response, list[Sighting]]:
    try:
        response = await fetcher.request(source.method, source.url())
    except (aiohttp.ClientError, TimeoutError) as error:
        raise PollError(f"{type(error).__name__} {error}".strip()) from error
    if response.status == 403:
        raise PollError("HTTP 403, so the site doesn't want us here. Stopped.", fatal=True)
    if response.status in (429, 503):
        retry_after = response.headers.get("retry-after", "")
        pause = float(retry_after) if retry_after.isdigit() else 30.0
        raise PollError(f"HTTP {response.status}, backing off for {pause:.0f}s", pause=pause)
    if response.status not in source.ok_statuses:
        raise PollError(f"HTTP {response.status}")
    try:
        return response, source.parse(response)
    except (ValueError, KeyError, TypeError) as error:
        raise PollError(f"unexpected response: {error!r}") from error


def record_items(
    w: Watch,
    items: list[Sighting],
    response: Response,
    journal: Journal,
    sent: datetime,
    previous: datetime | None,
) -> None:
    if w.known is None:
        w.known = {item.key for item in items}
        journal.write("ready", ticker=w.ticker, source=w.source.name, listed=len(items))
        journal.say(f"{w.ticker} {w.source.name}: ready, {len(items)} listed")
        return
    for item in items:
        if item.key in w.known:
            continue
        w.known.add(item.key)
        seen = journal.now()
        # The item appeared between the previous poll going out and this response arriving
        journal.write(
            "new",
            ticker=w.ticker,
            source=w.source.name,
            **asdict(item),
            seen=seen,
            sent=sent,
            previous_sent=previous,
            response_ms=round(response.elapsed * 1000),
            headers={
                name: response.headers[name] for name in CACHE_HEADERS if name in response.headers
            },
        )
        if item.earnings and w.first is None:
            w.first, w.first_title = seen, item.title
        journal.say(f"{w.ticker} {w.source.name}: NEW{' *' if item.earnings else ''} {item.title}")


async def watch(w: Watch, fetcher: Fetcher, journal: Journal, end: datetime) -> None:
    previous: datetime | None = None
    while (sent := journal.now()) < end:
        next_poll = time.monotonic() + w.source.interval
        w.polls += 1
        try:
            response, items = await poll(w.source, fetcher)
        except PollError as error:
            w.errors += 1
            journal.write("error", ticker=w.ticker, source=w.source.name, message=str(error))
            if str(error) != w.problem:
                journal.say(f"{w.ticker} {w.source.name}: {error}")
            w.problem = str(error)
            if error.fatal:
                w.stopped = str(error)
                return
            next_poll += error.pause
        else:
            if w.problem:
                journal.say(f"{w.ticker} {w.source.name}: working again")
                w.problem = None
            w.elapsed.append(response.elapsed)
            record_items(w, items, response, journal, sent, previous)
            previous = sent
        await asyncio.sleep(max(0.0, next_poll - time.monotonic()))


def summarize(target: Target, watches: list[Watch], journal: Journal) -> None:
    def signed(seconds: float) -> str:
        return f"{seconds:+.1f}s"

    rows = sorted(watches, key=lambda w: w.first or datetime.max.replace(tzinfo=UTC))
    winner = rows[0].first if rows else None
    width = max(len(w.source.name) for w in watches)
    lines = [
        f"{target.ticker}: expected {_clock(target.expected)} ET. First seen is the first item"
        " that looks like the release.",
        f"  {'source':<{width}}  {'first seen':<12}  {'vs expected':>11}  {'vs first':>9}"
        f"  {'polls':>5}  {'errors':>6}  {'median':>7}",
    ]
    for w in rows:
        median = f"{statistics.median(w.elapsed) * 1000:.0f} ms" if w.elapsed else "-"
        if w.first and winner:
            seen = _clock(w.first)
            late = signed((w.first - target.expected).total_seconds())
            behind = signed((w.first - winner).total_seconds())
        else:
            seen, late, behind = ("stopped" if w.stopped else "not seen"), "", ""
        lines.append(
            f"  {w.source.name:<{width}}  {seen:<12}  {late:>11}  {behind:>9}"
            f"  {w.polls:>5}  {w.errors:>6}  {median:>7}"
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
                "polls": w.polls,
                "errors": w.errors,
                "stopped": w.stopped,
                "median_ms": round(statistics.median(w.elapsed) * 1000) if w.elapsed else None,
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


async def record_target(target: Target, fetcher: Fetcher, journal: Journal) -> None:
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

    watches = [Watch(target.ticker, source) for source in target.sources()]
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
                group.create_task(watch(w, fetcher, journal, end))
    finally:
        summarize(target, watches, journal)


async def record(plan: Plan, sec_user_agent: str, log_path: Path) -> None:
    with log_path.open("a", encoding="utf-8") as file:
        journal = Journal(file)
        journal.write("start", targets=[t.ticker for t in plan.target])
        await sync_clock(journal)
        journal.say(f"Logging to {log_path}. Press Ctrl+C to stop.")
        async with Fetcher(sec_user_agent) as fetcher, asyncio.TaskGroup() as group:
            for target in plan.target:
                group.create_task(record_target(target, fetcher, journal))


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
            record(plan, settings.sec_user_agent, args.log or args.plan.with_suffix(".jsonl"))
        )


if __name__ == "__main__":
    main()
