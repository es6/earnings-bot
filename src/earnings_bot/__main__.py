"""Entry point: `uv run earnings-bot [SCHEDULE]` or `python -m earnings_bot [SCHEDULE]`."""

import argparse
import logging
import sys
import tomllib
from pathlib import Path

import discord
from pydantic import ValidationError

from earnings_bot.bot import EarningsBot
from earnings_bot.config import Settings
from earnings_bot.recorder import configure_console, load_plan
from earnings_bot.version import release_version

log = logging.getLogger("earnings_bot")


def main() -> None:
    parser = argparse.ArgumentParser(prog="earnings-bot", description="Run the Discord bot.")
    parser.add_argument("--version", action="version", version=release_version())
    parser.add_argument(
        "schedule",
        nargs="?",
        type=Path,
        help="a schedule file, or a schedule directory to watch continuously",
    )
    args = parser.parse_args()
    configure_console()

    # Pyright can't see that BaseSettings reads its fields from the environment
    settings = Settings()  # pyright: ignore[reportCallIssue]
    discord.utils.setup_logging(level=logging.getLevelNamesMapping()[settings.log_level])
    if args.schedule:
        missing = [
            name
            for name, value in [
                ("DISCORD_CHANNEL_ID", settings.discord_channel_id),
                ("SEC_USER_AGENT", settings.sec_user_agent),
            ]
            if value is None
        ]
        if missing:
            sys.exit(f"To watch a schedule, set {' and '.join(missing)} in .env")
        try:
            if not args.schedule.is_dir():
                load_plan(args.schedule)  # validate single files before starting
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
            sys.exit(f"Can't read {args.schedule}: {error}")
        if settings.discord_status_channel_id == settings.discord_channel_id:
            sys.exit("DISCORD_STATUS_CHANNEL_ID must differ from DISCORD_CHANNEL_ID")
        if settings.anthropic_api_key is None:
            log.warning("ANTHROPIC_API_KEY isn't set, so posts won't get a summary")
        if settings.fmp_api_key is None:
            log.warning("FMP_API_KEY isn't set, so summaries won't have estimates")

    bot = EarningsBot(settings, args.schedule)
    # log_handler=None because logging is already set up above
    bot.run(settings.discord_token.get_secret_value(), log_handler=None)


if __name__ == "__main__":
    main()
