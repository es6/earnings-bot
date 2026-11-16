from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest
from discord import app_commands

from earnings_bot.bot import EarningsBot
from earnings_bot.commands import ALL_COMMANDS, ping
from earnings_bot.config import Settings
from earnings_bot.version import release_version


async def test_ping_replies_privately_with_latency() -> None:
    interaction = MagicMock()
    interaction.client.latency = 0.042
    interaction.response.send_message = AsyncMock()

    # .callback is the original function; pyright can't narrow discord.py's union type for it
    await ping.callback(interaction)  # pyright: ignore[reportCallIssue]

    interaction.response.send_message.assert_awaited_once_with(
        "Pong! Gateway latency: 42 ms", ephemeral=True
    )


async def test_the_bot_registers_ping_in_its_command_tree() -> None:
    bot = EarningsBot(Settings.model_validate({"discord_token": "test"}))

    assert ping in ALL_COMMANDS
    assert bot.tree.get_command("ping") is ping


async def test_watch_status_shows_build_separately_from_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("earnings_bot.bot.build_revision", lambda: "0123456789")
    bot = EarningsBot(Settings.model_validate({"discord_token": "test"}))
    send = AsyncMock()
    interaction = cast(discord.Interaction, Mock(response=Mock(send_message=send)))

    command = bot.tree.get_command("status")
    assert isinstance(command, app_commands.Command)
    # A command made from a bound method keeps its instance as `binding`; pyright can't type it
    await command.callback(command.binding, interaction)  # pyright: ignore[reportCallIssue, reportArgumentType]

    send.assert_awaited_once_with(
        f"**Earnings Bot {release_version()}**\nBuild `0123456789`\n"
        "Earnings monitoring is not configured.",
        ephemeral=True,
    )
