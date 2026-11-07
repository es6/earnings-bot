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
from earnings_bot.unattended import DirectoryWatcher, watch_directory

log = logging.getLogger(__name__)


class EarningsBot(discord.Client):
    def __init__(self, settings: Settings, schedule: Path | None = None) -> None:
        # Slash commands don't need any privileged intents
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.schedule = schedule
        self.directory_mode = schedule is not None and schedule.is_dir()
        self.tree = app_commands.CommandTree(self)
        for command in ALL_COMMANDS:
            self.tree.add_command(command)
        self.watcher: asyncio.Task[None] | None = None
        self.directory_watcher: DirectoryWatcher | None = None
        self.tree.add_command(
            app_commands.Command(
                name="watch_status", description="Check earnings monitoring", callback=self._status
            )
        )

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
        summarizer = Summarizer(key.get_secret_value()) if key else None
        api_key = fmp_key.get_secret_value() if fmp_key else None
        log.info("Watching %s, posting to #%s", schedule, getattr(channel, "name", channel))
        if self.directory_mode:
            status_channel = None
            if settings.discord_status_channel_id is not None:
                try:
                    candidate = await self.fetch_channel(settings.discord_status_channel_id)
                except discord.HTTPException:
                    log.exception("Can't open the status channel. Continuing with local logs.")
                else:
                    if isinstance(candidate, discord.abc.Messageable):
                        status_channel = candidate
                    else:
                        log.error("Status channel cannot receive messages. Using local logs.")
            if self.user is None:
                raise RuntimeError("Discord user unavailable after startup")
            await watch_directory(
                schedule,
                channel,
                settings.discord_channel_id,
                self.user.id,
                settings.sec_user_agent,
                summarizer,
                api_key,
                status_channel,
                self._watch_ready,
            )
            return
        await watch_schedule(
            load_plan(schedule),
            schedule.with_suffix(".jsonl"),
            channel,
            settings.sec_user_agent,
            summarizer,
            api_key,
        )
        log.info("Every window in %s has closed", schedule)

    def _watch_ready(self, watcher: DirectoryWatcher) -> None:
        self.directory_watcher = watcher

    async def _status(self, interaction: discord.Interaction) -> None:
        if self.watcher is None:
            status = "Earnings monitoring is not configured."
        elif self.watcher.done():
            status = "Earnings monitoring is stopped."
        elif self.directory_watcher is None:
            status = f"Earnings watcher is running for {self.schedule}."
        else:
            status = self.directory_watcher.health()
        await interaction.response.send_message(status, ephemeral=True)

    def _watch_ended(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()):
            log.error("Watching the schedule stopped", exc_info=error)
            if self.directory_mode:
                # Let the Windows supervisor restart the entire process.
                asyncio.create_task(self.close())  # noqa: RUF006
