"""The Discord client: holds the Gateway connection and the slash command tree."""

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands

from earnings_bot.commands import ALL_COMMANDS
from earnings_bot.config import Settings
from earnings_bot.pipeline import watch_schedule
from earnings_bot.recorder import load_plan
from earnings_bot.summary import Summarizer

log = logging.getLogger(__name__)


class EarningsBot(discord.Client):
    def __init__(self, settings: Settings, schedule: Path | None = None) -> None:
        # Slash commands don't need any privileged intents
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.schedule = schedule
        self.tree = app_commands.CommandTree(self)
        for command in ALL_COMMANDS:
            self.tree.add_command(command)
        self.watcher: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        # Runs once after login, before the Gateway connection opens
        if self.settings.discord_guild_id is not None:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d command(s) to test server %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d command(s) globally", len(synced))
        if self.schedule is not None:
            self.watcher = asyncio.create_task(self._watch(self.schedule))
            self.watcher.add_done_callback(self._watch_ended)

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def _watch(self, schedule: Path) -> None:
        settings = self.settings
        # main() checks both before starting the bot
        if settings.discord_channel_id is None or settings.sec_user_agent is None:
            raise RuntimeError("DISCORD_CHANNEL_ID and SEC_USER_AGENT are required")
        await self.wait_until_ready()
        channel = await self.fetch_channel(settings.discord_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError(f"Channel {settings.discord_channel_id} can't take messages")
        key = settings.anthropic_api_key
        fmp_key = settings.fmp_api_key
        log.info("Watching %s, posting to #%s", schedule, getattr(channel, "name", channel))
        await watch_schedule(
            load_plan(schedule),
            schedule.with_suffix(".jsonl"),
            channel,
            settings.sec_user_agent,
            Summarizer(key.get_secret_value()) if key else None,
            fmp_key.get_secret_value() if fmp_key else None,
        )
        log.info("Every window in %s has closed", schedule)

    def _watch_ended(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()):
            log.error("Watching the schedule stopped", exc_info=error)
