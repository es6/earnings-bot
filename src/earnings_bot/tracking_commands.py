"""Commands for the live earnings calendar."""

from collections.abc import Callable

import discord
from discord import app_commands

from earnings_bot.calendar import (
    calendar_line,
    describe,
    manual_target,
    session,
    upcoming_releases,
)
from earnings_bot.formatting import BLUE, bullets
from earnings_bot.issuers import symbol
from earnings_bot.recorder import ET
from earnings_bot.track_progress import PROBLEM
from earnings_bot.unattended import DirectoryWatcher

OFFLINE = "⚠️ The earnings calendar is available when the directory watcher is running."
SCHEDULED = "📄"  # from a schedule file, not tracked with a scorecard
LEGEND = (
    "🟢 ready · 🔵 researching · 🟡 not ready or not verified fresh · 🔴 problem · "
    "📄 schedule file · "
    "times in your time zone"
)
EMBED_LIMIT = 6000  # Discord's limit for one embed's text


def upcoming_embed(watcher: DirectoryWatcher) -> discord.Embed:
    """The week's releases as a compact calendar: one line each, grouped by day and market
    session, with the scorecard's readiness. What needs attention comes first; details stay on
    each ticker's scorecard."""
    now = watcher.journal.now()
    releases, pending = upcoming_releases(watcher.calendar.entries(), watcher.planned, now)
    embed = discord.Embed(title="📅 Earnings in the next 7 days", color=BLUE)
    groups: dict[str, list[str]] = {}
    problems = []
    for target, evidence in releases:
        dot = watcher.progress.readiness(target.cik) if watcher.progress else None
        if dot == PROBLEM:
            problems.append(f"**{target.ticker}**: see its scorecard")
        local = target.expected.astimezone(ET)
        heading = f"{local:%a, %b} {local.day} · {session(target.expected)}"
        groups.setdefault(heading, []).append(calendar_line(target, evidence, dot or SCHEDULED))
    attention = problems + ([f"No date yet: {', '.join(pending)}"] if pending else [])
    if attention:
        embed.add_field(name="⚠️ Needs attention", value=bullets(attention, bullet=""), inline=False)
    for name, lines in groups.items():
        if len(embed.fields) == 25:  # Discord's field limit
            break
        embed.add_field(name=name, value=bullets(lines, bullet=""), inline=False)
    if not releases:
        embed.description = "No releases expected in the next 7 days."
    embed.set_footer(text=LEGEND)
    # A very full week keeps its first days whole rather than cutting lines mid-way
    dropped = 0
    while len(embed) > EMBED_LIMIT and len(embed.fields) > 1:
        embed.remove_field(len(embed.fields) - 1)
        dropped += 1
    if dropped:
        embed.set_footer(text=f"{dropped} later day(s) didn't fit · {LEGEND}")
    return embed


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

    def _watcher(self, interaction: discord.Interaction) -> DirectoryWatcher | None:
        watcher = self.get_watcher()
        if watcher and watcher.guild_id == interaction.guild_id:
            return watcher
        return None

    @app_commands.guild_only()
    @app_commands.describe(
        ticker="Stock symbol of a company that files with SEC",
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
        # Looking up a company outside the catalog can take longer than Discord's 3 seconds
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self._track(interaction, symbol(ticker), when, period)
        await interaction.followup.send(message, ephemeral=True)

    async def _track(
        self,
        interaction: discord.Interaction,
        ticker: str,
        when: str | None,
        period: str | None,
    ) -> str:
        watcher = self._watcher(interaction)
        if watcher is None:
            return OFFLINE
        now = watcher.journal.now()
        try:
            if when is not None or period is not None:
                if not interaction.permissions.manage_guild:
                    raise ValueError("Manage Server is required to set a release-time override.")
                if not when or not period:
                    raise ValueError(
                        "Supply both a release time and a fiscal period for an override."
                    )
            existing = watcher.calendar.find(ticker)
            company = (
                existing.company
                if existing
                else await watcher.issuers.company(ticker, now.astimezone(ET).date())
            )
            override = (
                manual_target(ticker, when, period, now, company) if when and period else None
            )
            row, added = watcher.calendar.add(ticker, company)
            watcher.dates.changed.set()
            if override:
                watcher.calendar.set_target(override, "Manual override")
                message = f"📌 {describe(override, 'Manual override')}"
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
                state = "✅ Now tracking" if added else "👍 Already tracking"
                message = f"{state} **{row.ticker}** · {row.company.name}"
                if target:
                    message += f"\n{describe(target, 'Scheduled')}"
            if watcher.progress:
                watcher.progress.changed.set()
                channel_id = watcher.progress.store.channel_id
                message += f"\nFollow the research live in <#{channel_id}>."
        except ValueError as error:
            message = f"⚠️ {error}"
        return message

    @app_commands.guild_only()
    async def upcoming(self, interaction: discord.Interaction) -> None:
        watcher = self._watcher(interaction)
        if watcher is None:
            await interaction.response.send_message(OFFLINE, ephemeral=True)
            return
        embed = upcoming_embed(watcher)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def untrack(self, interaction: discord.Interaction, ticker: str) -> None:
        watcher = self._watcher(interaction)
        ticker = symbol(ticker)
        try:
            if watcher is None:
                raise LookupError(OFFLINE)
            if not interaction.permissions.manage_guild:
                raise ValueError("Manage Server is required to remove a ticker.")
            row = watcher.calendar.find(ticker)
            removed = row is not None and watcher.calendar.remove(row.cik)
            if row and removed and watcher.progress:
                watcher.progress.removed[row.cik] = ticker
                watcher.progress.changed.set()
            message = (
                f"🗑️ Removed **{ticker}** from the calendar."
                if removed
                else f"**{ticker}** wasn't on the calendar."
            )
        except LookupError:
            message = OFFLINE
        except ValueError as error:
            message = f"⚠️ {error}"
        await interaction.response.send_message(message, ephemeral=True)
