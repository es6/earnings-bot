"""The Discord client: holds the Gateway connection and the slash command tree."""

import asyncio
import logging
import subprocess
from functools import partial
from pathlib import Path

import discord
from discord import app_commands

from earnings_bot.browser import sweep_leftover_chrome
from earnings_bot.commands import ALL_COMMANDS
from earnings_bot.config import Settings
from earnings_bot.pipeline import watch_schedule
from earnings_bot.recorder import load_plan
from earnings_bot.summary import summarizer_from
from earnings_bot.tracking_commands import TrackingCommands
from earnings_bot.unattended import DirectoryWatcher, watch_directory
from earnings_bot.version import build_revision, release_version

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
                name="status",
                description="Check that earnings monitoring is healthy",
                callback=self._status,
            )
        )
        # The old name, while people get used to /status
        self.tree.add_command(
            app_commands.Command(
                name="watch_status", description="Old name for /status", callback=self._status
            )
        )

        if self.directory_mode:
            TrackingCommands(self._calendar_watcher).register(self.tree)

    def _calendar_watcher(self) -> DirectoryWatcher | None:
        if self.watcher is None or self.watcher.done():
            return None
        return self.directory_watcher

    async def setup_hook(self) -> None:
        guild_ids = await self._server_guilds()
        if guild_ids:
            # Each configured server gets the commands at once; a global sync takes up to an hour
            for guild_id in guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d command(s) to server %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d command(s) globally", len(synced))
        if self.schedule is not None:
            try:
                ended, removed = await asyncio.to_thread(sweep_leftover_chrome)
                if ended or removed:
                    log.info(
                        "Ended %d Chrome processes a previous run left and removed %d old profiles",
                        ended,
                        removed,
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                log.warning("Couldn't check for Chrome a previous run left: %r", error)
            self.watcher = asyncio.create_task(self._watch(self.schedule))
            self.watcher.add_done_callback(self._watch_ended)

    async def _server_guilds(self) -> list[int]:
        """The test server, if set, and the server of every other alert channel. The
        deployment's own channel is in the test server."""
        guild_ids = (
            [self.settings.discord_guild_id] if self.settings.discord_guild_id is not None else []
        )
        channel_ids = (
            self.settings.alert_channel_ids
            if self.settings.discord_guild_id is None
            else self.settings.discord_server_channel_ids
        )
        for channel_id in channel_ids:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                log.exception("Can't open alert channel %s", channel_id)
                continue
            if isinstance(channel, discord.abc.GuildChannel | discord.Thread):
                guild_ids.append(channel.guild.id)
        return list(dict.fromkeys(guild_ids))

    async def _other_channels(self) -> list[discord.TextChannel | discord.Thread]:
        """The other servers' alert channels that can take the bot's messages."""
        channels: list[discord.TextChannel | discord.Thread] = []
        for channel_id in self.settings.alert_channel_ids[1:]:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                log.exception("Can't open alert channel %s; that server gets no alerts", channel_id)
                continue
            if isinstance(channel, discord.TextChannel | discord.Thread):
                channels.append(channel)
            else:
                log.error("Alert channel %s can't take messages; that server gets none", channel_id)
        return channels

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, release_version())

    async def _watch(self, schedule: Path) -> None:
        settings = self.settings
        if settings.discord_channel_id is None or settings.sec_user_agent is None:
            raise RuntimeError("DISCORD_CHANNEL_ID and SEC_USER_AGENT are required")
        await self.wait_until_ready()
        channel = await self.fetch_channel(settings.discord_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise RuntimeError(f"Channel {settings.discord_channel_id} can't take messages")
        fmp_key = settings.fmp_api_key
        summarizer = summarizer_from(settings)
        api_key = fmp_key.get_secret_value() if fmp_key else None
        # Reading the channel is about the cheapest request there is, and goes over the same
        # connection the alert will use
        ping = partial(self.fetch_channel, settings.discord_channel_id)
        log.info("Watching %s, posting to #%s", schedule, getattr(channel, "name", channel))
        if self.directory_mode:
            status_channel = None
            if settings.discord_status_channel_id is not None:
                try:
                    candidate = await self.fetch_channel(settings.discord_status_channel_id)
                except discord.HTTPException:
                    log.exception("Can't open the status channel. Continuing with local logs.")
                else:
                    if (
                        isinstance(candidate, discord.abc.Messageable)
                        and isinstance(candidate, discord.abc.GuildChannel | discord.Thread)
                        and isinstance(channel, discord.abc.GuildChannel | discord.Thread)
                        and candidate.guild.id == channel.guild.id
                    ):
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
                ping,
                driver_profiles=settings.live_driver_profiles,
                other_channels=await self._other_channels(),
            )
            return
        await watch_schedule(
            load_plan(schedule),
            schedule.with_suffix(".jsonl"),
            channel,
            settings.sec_user_agent,
            summarizer,
            ping,
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
            watcher = self.directory_watcher
            embed = watcher.health_embed(
                release_version(), build_revision(), watcher.server_for(interaction.guild_id)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        lines = [f"**Earnings Bot {release_version()}**"]
        if revision := build_revision():
            lines.append(f"Build `{revision}`")
        lines.append(status)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    def _watch_ended(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()):
            log.error("Watching the schedule stopped", exc_info=error)
            if self.directory_mode:
                # Let the Windows supervisor restart the entire process.
                asyncio.create_task(self.close())  # noqa: RUF006
