"""An alarm that doesn't depend on the bot: posts to a Discord webhook when a release window is
open and the journal shows a route has stopped polling, is being refused, or the bot has gone
quiet.

`uv run python -m earnings_bot.watchdog SCHEDULES` checks every 30 seconds. It reads the
bot's journal (`SCHEDULES/watch.jsonl`), not whether the bot's process exists: a process can
be alive with a stalled route. Windows come from the schedule files and the tracking calendar,
as the restart check reads them, so a bot that is down when a window opens still raises an
alarm. It is not a second bot: it never connects to Discord's gateway, and posts only through
the webhook in WATCHDOG_WEBHOOK_URL.
"""

import argparse
import json
import sys
import time
import traceback
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.formatting import clock, duration
from earnings_bot.recorder import configure_console
from earnings_bot.restart import schedule_targets

CHECK_SECONDS = 30.0
# Sources start polling within seconds of a window opening; give them this long
GRACE = timedelta(minutes=2)
# A route counts as down when it hasn't answered for this long, or five of its own poll
# intervals if that's longer. Watches report progress every 30 s (recorder.PROGRESS_SECONDS).
QUIET = timedelta(seconds=90)
# During a window the journal gets several lines a second. Silence this long means the bot
# is down or hung.
SILENT = timedelta(minutes=2)
# How much of the journal to read at start; it grows by megabytes on a release day
START_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Window:
    key: str
    ticker: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Progress:
    at: datetime
    interval: float
    answered: datetime | None
    problem: str | None
    done: bool


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def planned_windows(directory: Path, channel_id: int) -> list[Window]:
    """Every release window the bot would watch: schedule files plus tracked companies."""
    targets, _ = schedule_targets(directory)
    with DeliveryStore(directory / ".earnings-state.sqlite3", channel_id) as store:
        planned = Calendar(store).plan(targets)
    return [Window(key, t.ticker, *t.window) for key, t in planned.items()]


def post_webhook(url: str, text: str) -> None:
    body = json.dumps({"content": text[:2000], "allowed_mentions": {"parse": []}}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "earnings-bot-watchdog"},
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


class Watchdog:
    def __init__(
        self,
        journal: Path,
        windows: Callable[[], list[Window]],
        post: Callable[[str], None],
    ) -> None:
        self.journal, self.windows, self.post = journal, windows, post
        self.offset: int | None = None
        self.partial = b""
        self.last_line: datetime | None = None
        self.progress: dict[tuple[str, str], Progress] = {}  # latest, by (ticker, source)
        self.sources: dict[tuple[str, datetime], list[str]] = {}  # by (ticker, window start)
        self.raised: dict[tuple[str, str, str], Window] = {}  # alarms posted and not yet cleared

    def read(self) -> None:
        """Takes in the journal lines written since the last read."""
        try:
            size = self.journal.stat().st_size
        except FileNotFoundError:
            return
        if self.offset is None or size < self.offset:
            self.offset = max(0, size - START_BYTES)
            skip = self.offset > 0  # the first line read may start mid-line
        else:
            skip = False
        with self.journal.open("rb") as file:
            file.seek(self.offset)
            data = self.partial + file.read()
        self.offset += len(data) - len(self.partial)
        lines = data.split(b"\n")
        self.partial = lines.pop()
        if skip and lines:
            lines.pop(0)
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            self._take(record)

    def _take(self, record: dict[str, Any]) -> None:
        at = _time(record.get("at"))
        if at is None:
            return
        self.last_line = max(self.last_line or at, at)
        ticker, event = record.get("ticker"), record.get("event")
        if event == "progress" and ticker and record.get("source"):
            self.progress[ticker, record["source"]] = Progress(
                at,
                float(record.get("interval") or 1.0),
                _time(record.get("answered")),
                record.get("problem"),
                bool(record.get("done")),
            )
        elif event == "window" and ticker and (start := _time(record.get("start"))):
            self.sources[ticker, start] = list(record.get("sources") or [])

    def alarms(self, now: datetime) -> dict[tuple[str, str, str], tuple[Window, str]]:
        """What is wrong right now, by (release, route, kind)."""
        found: dict[tuple[str, str, str], tuple[Window, str]] = {}
        for window in self.windows():
            if not window.start + GRACE <= now < window.end:
                continue
            until = f"Window open until {clock(window.end)}."
            silence = now - self.last_line if self.last_line else None
            if silence is None or silence > SILENT:
                quiet = f"for {duration(silence.total_seconds())}" if silence else "at all"
                text = (
                    f"🚨 **{window.ticker}**: nothing written to the bot's journal {quiet}. "
                    f"The bot may be down. {until}"
                )
                found[window.key, "", "silent"] = (window, text)
                continue
            routes = {
                source: progress
                for (ticker, source), progress in self.progress.items()
                if ticker == window.ticker and progress.at >= window.start
            }
            for source in self.sources.get((window.ticker, window.start), []):
                if source not in routes:
                    text = (
                        f"🚨 **{window.ticker}** · `{source}`: no poll recorded since the window "
                        f"opened at {clock(window.start)}. {until}"
                    )
                    found[window.key, source, "never"] = (window, text)
            for source, progress in routes.items():
                if progress.done:
                    continue
                last = max(progress.answered or window.start, window.start)
                limit = max(QUIET, timedelta(seconds=5 * progress.interval))
                if now - last > limit:
                    why = f" Last error: {progress.problem}." if progress.problem else ""
                    text = (
                        f"🚨 **{window.ticker}** · `{source}`: no answer for "
                        f"{duration((now - last).total_seconds())}.{why} {until}"
                    )
                    found[window.key, source, "quiet"] = (window, text)
        return found

    def check(self, now: datetime) -> None:
        """Reads the journal, posts each new alarm once, and says when one clears while its
        window is still open."""
        self.read()
        current = self.alarms(now)
        for key, (window, text) in current.items():
            if key not in self.raised and self._send(text):
                self.raised[key] = window
        for key, window in list(self.raised.items()):
            if key in current:
                continue
            del self.raised[key]
            if window.start <= now < window.end:
                route = f" · `{key[1]}`" if key[1] else ""
                self._send(f"✅ **{window.ticker}**{route}: polling again.")

    def check_safely(self, now: datetime) -> bool:
        """`check`, logging any failure instead of raising, so one bad check, such as the
        calendar database being busy, doesn't end the watch. False when it failed."""
        try:
            self.check(now)
        except Exception:
            print(f"{datetime.now(UTC):%H:%M:%S} check failed:", flush=True)
            traceback.print_exc()
            sys.stderr.flush()
            return False
        return True

    def _send(self, text: str) -> bool:
        try:
            self.post(text)
        except OSError as error:  # urllib's errors, timeouts included
            print(f"{datetime.now(UTC):%H:%M:%S} webhook post failed: {error}", flush=True)
            return False
        print(f"{datetime.now(UTC):%H:%M:%S} posted: {text}", flush=True)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m earnings_bot.watchdog",
        description="Post an alarm when a release window is open and polling has stopped.",
    )
    parser.add_argument("schedules", type=Path, help="the schedule directory the bot watches")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    args = parser.parse_args()
    configure_console()
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as error:
        sys.exit(f"Can't read the settings in .env: {error.error_count()} problem(s)")
    if settings.watchdog_webhook_url is None or settings.discord_channel_id is None:
        sys.exit("Set WATCHDOG_WEBHOOK_URL and DISCORD_CHANNEL_ID in .env")
    url = settings.watchdog_webhook_url.get_secret_value()
    channel = settings.discord_channel_id
    dog = Watchdog(
        args.schedules / "watch.jsonl",
        lambda: planned_windows(args.schedules, channel),
        lambda text: post_webhook(url, text),
    )
    print(f"{datetime.now(UTC):%H:%M:%S} watching {args.schedules} every {CHECK_SECONDS:g}s")
    while True:
        ok = dog.check_safely(datetime.now(UTC))
        if args.once:
            sys.exit(0 if ok else 1)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
