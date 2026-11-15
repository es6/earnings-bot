"""Commands for the live earnings calendar."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import discord
from discord import app_commands

from earnings_bot.calendar import (
    calendar_line,
    describe,
    manual_target,
    session,
    upcoming_releases,
)
from earnings_bot.formatting import AMBER, BLUE, GREEN, bullets, truncate
from earnings_bot.issuers import symbol
from earnings_bot.recorder import ET
from earnings_bot.track_progress import PROBLEM
from earnings_bot.track_test import (
    MISSING,
    UNSURE,
    TrackReport,
    compare,
    load_key,
    run_track_test,
    save,
)
from earnings_bot.unattended import DirectoryWatcher

OFFLINE = "⚠️ The earnings calendar is available when the directory watcher is running."
# A test stops in time to reply: Discord takes follow-ups for 15 minutes
TEST_SECONDS = 600
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


def checklist_embed(report: TrackReport, saved: Path) -> discord.Embed:
    """A test run's checklist for the member who ran it: each fact found, unsure or missing,
    the time each step took, any difference from the answer key, and where the full report
    is."""
    failed = bool(report.error) or any(mark == MISSING for mark, _, _ in report.checklist())
    color = AMBER if failed else GREEN
    embed = discord.Embed(title=f"🧪 /track test · {report.symbol}", color=color)
    lines = [f"{mark} **{label}**: {value}" for mark, label, value in report.checklist()]
    if report.error:
        lines.insert(0, f"🛑 Stopped: {report.error}")
    embed.description = truncate("\n".join(lines), 4096)
    steps = [f"{step.name}: {step.seconds:.0f} s · {step.outcome}" for step in report.steps]
    embed.add_field(name="⏱️ Steps", value=truncate("\n".join(steps) or "none", 1024), inline=False)
    if report.mismatches is not None:
        value = "\n".join(f"≠ {m}" for m in report.mismatches) or "Every fact matches: one-shot ✅"
        embed.add_field(name="🔑 Answer key", value=truncate(value, 1024), inline=False)
    embed.set_footer(
        text=f"Nothing was saved to the calendar · {UNSURE} unsure · report {saved.name}"
    )
    return embed


async def is_owner(client: discord.Client, user: discord.abc.User) -> bool:
    """Whoever owns the bot's Discord application, or any member of the team that owns it,
    the way discord.py's Bot.is_owner reads it. Nothing to configure, and no server role can
    take it away."""
    app = client.application or await client.application_info()
    if app.team is not None:
        return any(member.id == user.id for member in app.team.members)
    return app.owner.id == user.id


class TrackingCommands:
    def __init__(self, get_watcher: Callable[[], DirectoryWatcher | None]) -> None:
        self.get_watcher = get_watcher
        self.testing = asyncio.Lock()  # one /track test at a time

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
        test="Run all of /track's research from scratch as a test, saving nothing",
    )
    async def track(
        self,
        interaction: discord.Interaction,
        ticker: str,
        when: str | None = None,
        period: str | None = None,
        test: bool = False,
    ) -> None:
        # Looking up a company outside the catalog can take longer than Discord's 3 seconds
        await interaction.response.defer(ephemeral=True, thinking=True)
        if test:
            reply = await self._test(interaction, symbol(ticker))
            if isinstance(reply, discord.Embed):
                await interaction.followup.send(embed=reply, ephemeral=True)
            else:
                await interaction.followup.send(reply, ephemeral=True)
            return
        message = await self._track(interaction, symbol(ticker), when, period)
        await interaction.followup.send(message, ephemeral=True)

    async def _test(self, interaction: discord.Interaction, ticker: str) -> discord.Embed | str:
        """/track test mode (track_test): for members with Manage Server, and for the bot's
        owner in any server whatever their roles there (the owner's decision)."""
        watcher = self.get_watcher()
        if watcher is None:
            return OFFLINE
        # Manage Server counts in the bot's own server; the owner counts in any server
        admin = watcher.guild_id == interaction.guild_id and interaction.permissions.manage_guild
        if not admin and not await is_owner(interaction.client, interaction.user):
            return "⚠️ Test mode is for members with Manage Server and the bot's owner."
        if watcher.research_paused():
            return "⏳ A release is due in the next few minutes. Run the test after it."
        if self.testing.locked():
            return "⏳ Another /track test is running. Try again when it's done."
        async with self.testing:
            # Behind the live windows' polls, like any research
            with watcher.research_mode():
                report = await run_track_test(
                    ticker,
                    watcher.fetcher,
                    watcher.directory,
                    watcher.journal.now,
                    watcher.dates.fmp_api_key,
                    sixk=watcher.sixk,
                    busy=watcher.research_paused,
                    windows_open=watcher.busy,
                    chrome_live=watcher.chrome_live,
                    limit=TEST_SECONDS,
                )
            if (key := load_key(ticker)) is not None:
                report.mismatches = compare(report, key)
            saved = save(report, watcher.directory)
        return checklist_embed(report, saved)

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
