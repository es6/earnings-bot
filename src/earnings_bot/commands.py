"""Slash commands. Everything in ALL_COMMANDS is registered on the bot's command tree."""

import discord
from discord import app_commands


@app_commands.command(description="Check that the bot is online")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(interaction.client.latency * 1000)
    # ephemeral: only the person who ran the command sees the reply
    await interaction.response.send_message(
        f"Pong! Gateway latency: {latency_ms} ms", ephemeral=True
    )


ALL_COMMANDS = [ping]
