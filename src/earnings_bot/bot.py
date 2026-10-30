"""The Discord client: holds the Gateway connection and the slash command tree."""

import logging

import discord
from discord import app_commands

from earnings_bot.commands import ALL_COMMANDS
from earnings_bot.config import Settings

log = logging.getLogger(__name__)


class EarningsBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        # Slash commands don't need any privileged intents
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        for command in ALL_COMMANDS:
            self.tree.add_command(command)

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

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
