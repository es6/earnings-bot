from unittest.mock import AsyncMock, MagicMock

from earnings_bot.commands import ping


async def test_ping_replies_privately_with_latency() -> None:
    interaction = MagicMock()
    interaction.client.latency = 0.042
    interaction.response.send_message = AsyncMock()

    # .callback is the original function; pyright can't narrow discord.py's union type for it
    await ping.callback(interaction)  # pyright: ignore[reportCallIssue]

    interaction.response.send_message.assert_awaited_once_with(
        "Pong! Gateway latency: 42 ms", ephemeral=True
    )
