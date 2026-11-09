import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import discord

from earnings_bot.bot import EarningsBot
from earnings_bot.calendar import Calendar
from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.issuers import profile
from earnings_bot.known import KnownCompany
from earnings_bot.track_progress import TrackProgress
from tests.sec_data import LCID_CIK, LCID_SUBMISSIONS
from tests.test_unattended import FakeDiscord, FakeMessage


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


class WatchedDiscord(FakeDiscord):
    """Sets `sent` when a message goes out."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = asyncio.Event()

    async def send(self, content: str, *, nonce: str) -> FakeMessage:
        message = await super().send(content, nonce=nonce)
        self.sent.set()
        return message


async def test_the_audit_waits_for_the_investigation(tmp_path: Path) -> None:
    channel = WatchedDiscord()
    now = datetime(2026, 11, 9, 15, tzinfo=UTC)
    audited: list[str] = []
    asked = asyncio.Event()

    async def audit(ticker: str, company: KnownCompany, when: datetime) -> str:
        audited.append(company.evidence)
        asked.set()
        return "Last release | measured"

    lucid = profile("LCID", LCID_CIK, "Lucid Group, Inc.", LCID_SUBMISSIONS, date(2026, 11, 9))
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        calendar = Calendar(store)
        calendar.add("LCID", 1, now, lucid)
        progress = TrackProgress(
            calendar,
            store,
            cast(discord.abc.Messageable, channel),
            42,
            dict,
            lambda: now,
            lambda: False,
            audit,
        )
        task = asyncio.create_task(progress.run())
        try:
            await asyncio.wait_for(channel.sent.wait(), 5)
            [card] = channel.messages.values()
            assert card.content.splitlines()[-2:] == [
                lucid.evidence,
                "Looking for its IR site, feeds and newswire.",
            ]
            assert audited == []

            found = lucid.model_copy(
                update={"investigated": date(2026, 11, 9), "evidence": "Investigated Nov 09: X."}
            )
            calendar.save_profile(LCID_CIK, found)
            progress.changed.set()
            await asyncio.wait_for(asked.wait(), 5)
            # The audit uses what the investigation found
            assert audited == ["Investigated Nov 09: X."]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
