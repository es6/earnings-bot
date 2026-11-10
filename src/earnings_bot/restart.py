"""Whether the bot can restart now without missing a release.

`uv run python -m earnings_bot.restart SCHEDULES` prints each release window a restart would
interrupt, and exits with WINDOW_OPEN if there is one. scripts/update-bot.ps1 runs it first.
"""

import argparse
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.recorder import ET, Target, parse_plan

# The bot was watching again 30 s after a restart on Nov 9, 2026. This leaves room for a slow
# start before a window opens.
LEAD = timedelta(minutes=5)

WINDOW_OPEN = 3


def schedule_targets(directory: Path) -> tuple[dict[str, Target], list[str]]:
    """The releases in a directory's schedule files, read as the bot reads them, and the files
    it can't read."""
    targets: dict[str, Target] = {}
    problems: list[str] = []
    for path in sorted(path for path in directory.iterdir() if path.suffix == ".toml"):
        try:
            plan = parse_plan(path.read_bytes())
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
            problems.append(f"Can't read {path.name}: {error}")
            continue
        for target in plan.target:
            # Like the bot, the first file to define a release wins
            targets.setdefault(target.release_key, target)
    return targets, problems


def interrupted(targets: dict[str, Target], store: DeliveryStore, now: datetime) -> list[str]:
    """The windows a restart now would interrupt: open or opening within LEAD, and their alert not
    posted yet. A restart picks an alert already posted back up and keeps editing it."""
    reasons = []
    for key, target in sorted(targets.items(), key=lambda item: item[1].window):
        start, end = target.window
        if not start - LEAD <= now < end or store.window_closed(key, target.expected):
            continue
        posted = store.get(key)
        if posted is not None and posted.message_id is not None:
            continue
        state = "is open" if start <= now else f"opens at {start.astimezone(ET):%H:%M}"
        reasons.append(
            f"{target.ticker}'s window {state} and runs until {end.astimezone(ET):%H:%M} ET, "
            "with no alert posted yet"
        )
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m earnings_bot.restart",
        description="Say whether restarting the bot now would interrupt a release window.",
    )
    parser.add_argument("schedules", type=Path, help="the schedule directory the bot watches")
    args = parser.parse_args()
    # Pyright can't see that BaseSettings reads its fields from the environment
    settings = Settings()  # pyright: ignore[reportCallIssue]
    if settings.discord_channel_id is None:
        sys.exit("Set DISCORD_CHANNEL_ID in .env")

    targets, problems = schedule_targets(args.schedules)
    for problem in problems:
        print(problem)
    path = args.schedules / ".earnings-state.sqlite3"
    with DeliveryStore(path, settings.discord_channel_id) as store:
        # Tracked tickers' windows count too
        planned = Calendar(store).plan(targets)
        reasons = interrupted(planned, store, datetime.now(ET))
    for reason in reasons:
        print(reason)
    sys.exit(WINDOW_OPEN if reasons else 0)


if __name__ == "__main__":
    main()
