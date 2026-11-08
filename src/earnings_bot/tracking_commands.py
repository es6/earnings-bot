"""Commands for the live earnings calendar."""

from collections.abc import Callable

import discord
from discord import app_commands

from earnings_bot.calendar import company_cik, describe, manual_target, upcoming
from earnings_bot.unattended import DirectoryWatcher

OFFLINE = "The earnings calendar is available when the directory watcher is running."


class TrackingCommands:
    def __init__(self, get_watcher: Callable[[], DirectoryWatcher | None]) -> None:
        self.get_watcher = get_watcher

    def register(self, tree: app_commands.CommandTree) -> None:
        tree.add_command(
            app_commands.Command(
                name="track",
                description="Add an issuer to the earnings calendar",
                callback=self.track,
            )
        )
        tree.add_command(
            app_commands.Command(
                name="upcoming",
                description="See the next seven days of earnings",
                callback=self.upcoming,
            )
        )
        tree.add_command(
            app_commands.Command(
                name="untrack",
                description="Remove an issuer from the calendar",
                callback=self.untrack,
            )
        )

    @app_commands.guild_only()
    @app_commands.describe(
        ticker="Stock symbol in the ticker catalog",
        when="Admin override. ISO date and time, Eastern by default",
        period="Fiscal period of an override, such as FY2027Q3",
    )
    async def track(
        self,
        interaction: discord.Interaction,
        ticker: str,
        when: str | None = None,
        period: str | None = None,
    ) -> None:
        watcher = self.get_watcher()
        if watcher is None:
            await interaction.response.send_message(OFFLINE, ephemeral=True)
            return
        ticker = ticker.strip().upper()
        now = watcher.journal.now()
        try:
            company_cik(ticker)
            override = None
            if when is not None or period is not None:
                if not interaction.permissions.manage_guild:
                    raise ValueError("Manage Server is required to set a release-time override.")
                if not when or not period:
                    raise ValueError(
                        "Supply both a release time and a fiscal period for an override."
                    )
                override = manual_target(ticker, when, period, now)
            row, added = watcher.calendar.add(ticker, interaction.user.id, now)
            if override:
                watcher.calendar.set_target(override, "Manual override")
                message = describe(override, "Manual override")
            else:
                target = min(
                    (
                        item
                        for item in watcher.planned.values()
                        if item.cik == row.cik and item.window[1] > now
                    ),
                    key=lambda item: item.expected,
                    default=None,
                )
                state = "Tracking" if added else "Already tracking"
                next_release = (
                    describe(target, "Scheduled") if target else "Awaiting a confirmed date."
                )
                message = f"{state} **{row.ticker}**. {next_release}"
        except ValueError as error:
            message = str(error)
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.guild_only()
    async def upcoming(self, interaction: discord.Interaction) -> None:
        watcher = self.get_watcher()
        if watcher is None:
            message = OFFLINE
        else:
            rows = upcoming(watcher.calendar.entries(), watcher.planned, watcher.journal.now())
            message = "\n".join(rows) or "No earnings windows in the next seven days."
            if len(message) > 1950:
                message = message[:1950].rsplit("\n", 1)[0] + "\nMore releases are scheduled."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def untrack(self, interaction: discord.Interaction, ticker: str) -> None:
        watcher = self.get_watcher()
        ticker = ticker.strip().upper()
        try:
            if watcher is None:
                raise ValueError(OFFLINE)
            if not interaction.permissions.manage_guild:
                raise ValueError("Manage Server is required to remove a ticker.")
            removed = watcher.calendar.remove(company_cik(ticker))
            message = (
                f"Removed **{ticker}** from the calendar."
                if removed
                else f"**{ticker}** was already absent."
            )
        except ValueError as error:
            message = str(error)
        await interaction.response.send_message(message, ephemeral=True)
