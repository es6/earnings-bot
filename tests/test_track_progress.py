from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import discord

from earnings_bot.bot import EarningsBot
from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.known import KnownCompany
from earnings_bot.track_progress import TrackProgress
from tests.test_unattended import FakeDiscord


def test_directory_bot_registers_tracking_commands(tmp_path: Path) -> None:
    bot = EarningsBot(Settings.model_validate({"discord_token": "test"}), tmp_path)
    assert {"track", "upcoming", "untrack"} <= {command.name for command in bot.tree.get_commands()}


async def test_progress_resumes_after_discord_accepted_send(tmp_path: Path) -> None:
    channel = FakeDiscord()
    now = datetime(2026, 11, 7, tzinfo=UTC)

    async def audit(ticker: str, company: KnownCompany, when: datetime) -> str:
        raise AssertionError("The publisher should not start an audit")

    def progress(store: DeliveryStore) -> TrackProgress:
        return TrackProgress(
            Calendar(store),
            store,
            cast(discord.abc.Messageable, channel),
            42,
            dict,
            lambda: now,
            lambda: False,
            audit,
        )

    db = tmp_path / "state.db"
    with DeliveryStore(db, 7) as store:
        channel.lose_response = True
        await progress(store).publish(1045810, "Queued")
        assert store.pending()
    with DeliveryStore(db, 7) as store:
        await progress(store).publish(1045810, "Finished")
        assert not store.pending()
    assert channel.attempts == 1
    assert channel.messages[1].content == "Finished"
