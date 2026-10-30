"""Entry point: `uv run earnings-bot` or `python -m earnings_bot`."""

import logging

import discord

from earnings_bot.bot import EarningsBot
from earnings_bot.config import Settings


def main() -> None:
    # Pyright can't see that BaseSettings reads its fields from the environment
    settings = Settings()  # pyright: ignore[reportCallIssue]
    discord.utils.setup_logging(level=logging.getLevelNamesMapping()[settings.log_level])
    bot = EarningsBot(settings)
    # log_handler=None because logging is already set up above
    bot.run(settings.discord_token.get_secret_value(), log_handler=None)


if __name__ == "__main__":
    main()
