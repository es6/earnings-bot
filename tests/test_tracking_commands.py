"""/track, /untrack and /upcoming, from the slash command to the alert."""

import asyncio
import io
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from discord import app_commands

from earnings_bot import calendar as tracking_calendar
from earnings_bot.calendar import describe
from earnings_bot.clock import TrustedClock
from earnings_bot.delivery import DeliveryStore
from earnings_bot.fetch import Response
from earnings_bot.known import known_companies, to_target
from earnings_bot.pipeline import Releases
from earnings_bot.recorder import ET, Journal
from earnings_bot.track_progress import TrackProgress
from earnings_bot.tracking_commands import OFFLINE, TrackingCommands
from earnings_bot.unattended import DirectoryWatcher
from tests.burst_replay import LocalClock
from tests.fakes import RoutedFetcher
from tests.test_pipeline import FakePoster
from tests.test_unattended import FakeDiscord

GUILD = 555
PROGRESS_CHANNEL = 99
NOW = datetime(2026, 11, 12, 12, tzinfo=ET)
NVDA_CIK = 1045810


class FixedClock(TrustedClock):
    def __init__(self, now: datetime) -> None:
        super().__init__(servers=())
        self.moment = now

    def now(self) -> datetime:
        return self.moment


async def call(
    handler: Callable[..., Awaitable[None]],
    *args: str,
    user: int = 1,
    admin: bool = False,
    guild: int = GUILD,
    **options: str | None,
) -> str:
    """Runs a command handler as one server member and returns its reply."""
    replies: list[str] = []

    async def send_message(content: str, *, ephemeral: bool = False) -> None:
        assert ephemeral, "calendar replies are only for the member who asked"
        replies.append(content)

    interaction = SimpleNamespace(
        guild_id=guild,
        user=SimpleNamespace(id=user),
        permissions=discord.Permissions(manage_guild=admin),
        response=SimpleNamespace(send_message=send_message),
    )
    await handler(cast(discord.Interaction, interaction), *args, **options)
    [reply] = replies
    return reply


async def never_audit(ticker: str, when: datetime) -> str:
    raise AssertionError("these tests don't run the scorecard's source audit")


@pytest.fixture
def watcher(tmp_path: Path) -> Iterator[DirectoryWatcher]:
    """A directory watcher with no schedule files, as the bot builds it, at a fixed time."""
    fetcher = RoutedFetcher({})
    journal = Journal(io.StringIO(), FixedClock(NOW))
    with (
        DeliveryStore(tmp_path / "state.db", 7) as store,
        DeliveryStore(tmp_path / "progress.db", PROGRESS_CHANNEL) as progress_store,
    ):
        watcher = DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None, None),
            store,
            lambda message: None,
        )
        watcher.guild_id = GUILD
        watcher.progress = TrackProgress(
            watcher.calendar,
            progress_store,
            cast(discord.abc.Messageable, FakeDiscord()),
            42,
            lambda: watcher.planned,
            journal.now,
            watcher.busy,
            never_audit,
        )
        yield watcher


@pytest.fixture
def commands(watcher: DirectoryWatcher) -> TrackingCommands:
    return TrackingCommands(lambda: watcher)


def tracked(watcher: DirectoryWatcher) -> list[str]:
    return [row.ticker for row in watcher.calendar.entries()]


async def test_track_adds_a_catalog_ticker_once_and_wakes_the_calendar(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    assert watcher.progress

    reply = await call(commands.track, " nvda ")

    assert reply == "Tracking **NVDA**. Awaiting a confirmed date. Progress appears in <#99>."
    assert tracked(watcher) == ["NVDA"]
    # Date discovery and the scorecard look now, not at their next pass
    assert watcher.dates.changed.is_set()
    assert watcher.progress.changed.is_set()

    # A repeat names the next window still to come, not one that has closed
    closed = to_target("NVDA", datetime(2026, 8, 27, 16, 20, tzinfo=ET), 2027, 2)
    sooner = to_target("NVDA", datetime(2026, 11, 17, 16, 20, tzinfo=ET), 2027, 3)
    later = to_target("NVDA", datetime(2027, 2, 25, 16, 20, tzinfo=ET), 2027, 4)
    watcher.planned = {t.release_key: t for t in (later, closed, sooner)}

    reply = await call(commands.track, "NVDA", user=2)

    assert reply == (
        f"Already tracking **NVDA**. {describe(sooner, 'Scheduled')} Progress appears in <#99>."
    )
    assert tracked(watcher) == ["NVDA"]


async def test_a_ticker_outside_the_catalog_is_refused(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    assert await call(commands.track, "zzzz") == "ZZZZ is outside the current ticker catalog."
    assert await call(commands.track, "  ") == "That symbol is outside the current ticker catalog."
    assert tracked(watcher) == []
    assert not watcher.dates.changed.is_set()


@pytest.mark.parametrize(
    ("admin", "when", "period", "refusal"),
    [
        (False, "2026-11-17T16:20", "FY2027Q3", "Manage Server is required"),
        (False, None, "FY2027Q3", "Manage Server is required"),
        (True, "2026-11-17T16:20", None, "Supply both"),
        (True, None, "FY2027Q3", "Supply both"),
        (True, "", "FY2027Q3", "Supply both"),
        (True, "2026-11-17", "FY2027Q3", "Use an ISO date and time"),
        (True, "2026-11-17T16:20", "Q3", "Use an ISO date and time"),
        (True, "2026-11-01T16:20", "FY2027Q3", "Choose a future earnings release time"),
    ],
    ids=[
        "member",
        "member-period-only",
        "time-only",
        "period-only",
        "empty-time",
        "no-time-of-day",
        "bad-period",
        "past",
    ],
)
async def test_a_release_time_override_needs_manage_server_and_both_fields(
    watcher: DirectoryWatcher,
    commands: TrackingCommands,
    admin: bool,
    when: str | None,
    period: str | None,
    refusal: str,
) -> None:
    reply = await call(commands.track, "NVDA", admin=admin, when=when, period=period)

    assert reply.startswith(refusal)
    # Refused before anything is saved, so it doesn't use up one of the day's adds either
    assert tracked(watcher) == []
    assert watcher.store.db.execute("SELECT count(*) FROM tracking_adds").fetchone()[0] == 0


async def test_an_admin_override_pins_the_release_and_can_move_it(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    reply = await call(
        commands.track, "NVDA", admin=True, when="2026-11-17T16:20", period="FY2027Q3"
    )

    [row] = watcher.calendar.entries()
    assert row.target is not None
    assert row.target.expected == datetime(2026, 11, 17, 16, 20, tzinfo=ET)
    assert row.target.period == "FY2027Q3"
    assert row.evidence == "Manual override"
    assert reply == describe(row.target, "Manual override") + " Progress appears in <#99>."

    # Pinning an issuer that's already tracked moves its window
    await call(commands.track, "NVDA", admin=True, when="2026-11-17 16:25", period="FY2027Q3")
    [row] = watcher.calendar.entries()
    assert row.target and row.target.expected == datetime(2026, 11, 17, 16, 25, tzinfo=ET)


async def test_the_daily_limit_and_the_calendar_cap_are_explained(
    watcher: DirectoryWatcher, commands: TrackingCommands, monkeypatch: pytest.MonkeyPatch
) -> None:
    for ticker in ("AAPL", "AMD", "AMZN", "META", "MSFT"):
        assert (await call(commands.track, ticker)).startswith(f"Tracking **{ticker}**")

    reply = await call(commands.track, "NVDA")

    assert reply == "You can add up to 5 tickers per Eastern calendar day."
    assert "NVDA" not in tracked(watcher)
    # The limit is per member
    assert (await call(commands.track, "NVDA", user=2)).startswith("Tracking **NVDA**")

    # The catalog has fewer companies than the real cap, so lower it to reach it
    monkeypatch.setattr(tracking_calendar, "MAX_TRACKED", 6)
    assert await call(commands.track, "TSLA", user=3) == "This calendar already tracks 6 companies."
    assert len(tracked(watcher)) == 6


async def test_commands_from_another_server_or_with_the_watcher_off_answer_offline(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    other = GUILD + 1
    assert await call(commands.track, "NVDA", guild=other) == OFFLINE
    assert await call(commands.upcoming, guild=other) == OFFLINE
    assert await call(commands.untrack, "NVDA", admin=True, guild=other) == OFFLINE
    assert tracked(watcher) == []

    stopped = TrackingCommands(lambda: None)
    assert await call(stopped.track, "NVDA") == OFFLINE
    assert await call(stopped.upcoming) == OFFLINE
    assert await call(stopped.untrack, "NVDA", admin=True) == OFFLINE


async def test_untrack_needs_manage_server_and_tells_the_scorecard(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    assert watcher.progress
    await call(commands.track, "NVDA")
    watcher.progress.changed.clear()

    assert await call(commands.untrack, "nvda") == "Manage Server is required to remove a ticker."
    assert tracked(watcher) == ["NVDA"]
    assert not watcher.progress.changed.is_set()

    assert await call(commands.untrack, " nvda ", admin=True) == (
        "Removed **NVDA** from the calendar."
    )
    assert tracked(watcher) == []
    # So the scorecard is edited to say the issuer was removed
    assert watcher.progress.removed == {NVDA_CIK: "NVDA"}
    assert watcher.progress.changed.is_set()

    assert await call(commands.untrack, "NVDA", admin=True) == "**NVDA** was already absent."
    assert await call(commands.untrack, "ZZZZ", admin=True) == (
        "ZZZZ is outside the current ticker catalog."
    )


def test_only_managers_see_untrack_and_every_command_is_server_only() -> None:
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    TrackingCommands(lambda: None).register(tree)
    registered = {command.name: command for command in tree.get_commands()}

    assert isinstance(untrack := registered["untrack"], app_commands.Command)
    assert untrack.default_permissions == discord.Permissions(manage_guild=True)
    assert isinstance(track := registered["track"], app_commands.Command)
    assert track.default_permissions is None
    assert all(
        isinstance(command, app_commands.Command) and command.guild_only
        for command in registered.values()
    )


async def test_upcoming_lists_the_weeks_windows_and_issuers_awaiting_dates(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    assert await call(commands.upcoming) == "No earnings windows in the next seven days."

    for ticker in ("CRWD", "HD", "NVDA"):
        await call(commands.track, ticker)
    found = to_target("HD", datetime(2026, 11, 17, 6, tzinfo=ET), 2026, 3)
    evidence = "Issuer confirmed release time <https://ir.homedepot.com/news-releases/q3>"
    watcher.calendar.set_target(found, evidence)
    nvda = to_target("NVDA", datetime(2026, 11, 17, 16, 20, tzinfo=ET), 2027, 3)
    # A schedule file for an issuer nobody tracked, after this week
    aapl = to_target("AAPL", datetime(2026, 11, 30, 16, 30, tzinfo=ET), 2027, 1)
    watcher.planned = watcher.calendar.plan({t.release_key: t for t in (nvda, aapl)})

    reply = await call(commands.upcoming)

    assert reply.splitlines() == [
        describe(found, evidence),
        describe(nvda, "Schedule file"),
        "Awaiting dates: CRWD",
    ]


async def test_a_long_upcoming_list_stops_at_a_whole_line(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    targets = [
        to_target(ticker, NOW + timedelta(hours=i + 1), 2027, 1)
        for i, ticker in enumerate(known_companies())
    ]
    watcher.planned = {t.release_key: t for t in targets}

    lines = (await call(commands.upcoming)).splitlines()

    assert len("\n".join(lines)) <= 2000  # Discord's limit for a message
    assert lines[-1] == "More releases are scheduled."
    assert lines[:-1] == [describe(t, "Schedule file") for t in targets[: len(lines) - 1]]


PLTR_FEED = "investors.palantir.com/feed/PressRelease.svc/GetPressReleaseList"
RESULTS = {
    "PressReleaseId": 9001,
    "Headline": "Palantir Reports Q4 2026 Financial Results",
    "LinkToDetailPage": "/news-details/2027/palantir-reports-q4-2026",
    "PressReleaseDate": "02/02/2027 16:05:00",
    "Body": "<p>" + "Revenue grew 70% year over year to $1.4 billion. " * 40 + "</p>",
}


class ReleaseDay(RoutedFetcher):
    """Palantir's IR site, whose press release list shows its results from `released_at`
    (time.monotonic()) on. Everything else answers 404."""

    def __init__(self) -> None:
        super().__init__({})
        self.released_at = float("inf")

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        give_up_after: float | None = None,
        urgent: bool = False,
        *,
        allow_redirects: bool = True,
    ) -> Response:
        listed: list[dict[str, Any]] = [RESULTS] if time.monotonic() >= self.released_at else []
        self.routes = {PLTR_FEED: (200, json.dumps({"GetPressReleaseListResult": listed}).encode())}
        return await super().request(
            method, url, headers, give_up_after, urgent, allow_redirects=allow_redirects
        )


class ShiftedClock(LocalClock):
    """This PC's clock moved forward by `shift`, so a test can skip to the end of a window."""

    shift = timedelta()

    def now(self) -> datetime:
        return super().now() + self.shift


async def test_a_tracked_issuer_with_no_schedule_file_gets_one_alert(tmp_path: Path) -> None:
    clock = ShiftedClock()
    clock.sync()
    journal = Journal(io.StringIO(), clock)
    fetcher = ReleaseDay()
    channel = FakeDiscord()
    status: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:

        def watcher() -> DirectoryWatcher:
            releases = Releases(
                FakePoster, fetcher, journal, None, None, lambda t: channel.poster(store, t)
            )
            built = DirectoryWatcher(tmp_path, fetcher, journal, releases, store, status.append)
            built.guild_id = GUILD
            return built

        bot = watcher()
        # Soon enough that the window, which opens 10 minutes early, is already open
        when = (clock.now() + timedelta(minutes=5)).astimezone(ET)
        reply = await call(
            TrackingCommands(lambda: bot).track,
            "PLTR",
            admin=True,
            when=f"{when:%Y-%m-%dT%H:%M}",
            period="FY2026Q4",
        )
        assert "Manual override" in reply
        [row] = bot.calendar.entries()
        assert row.target is not None

        # Skip to 3 seconds before the window closes. The results come out 0.8 s in, after the
        # first poll has seen the list without them.
        clock.shift = row.target.window[1] - clock.now() - timedelta(seconds=3)
        fetcher.released_at = time.monotonic() + 0.8
        await bot.scan()
        assert [window.target for window in bot.windows.values()] == [row.target]
        await asyncio.gather(*(window.task for window in bot.windows.values()))

        [alert] = channel.messages.values()
        assert "**$PLTR earnings are out**" in alert.content
        assert "https://investors.palantir.com/news-details/2027/palantir-reports-q4-2026" in (
            alert.content
        )
        [report] = [line for line in status if line.startswith("PLTR window closed.")]
        assert report.startswith("PLTR window closed. q4_press first at")
        assert store.window_closed(row.target.release_key, row.target.expected)
        # Still tracked, for the next quarter's date
        assert tracked(bot) == ["PLTR"]

        # After a restart the closed window stays closed, without a second report
        status.clear()
        restarted = watcher()
        await restarted.scan()
        assert restarted.windows == {}
        assert status == []
        assert len(channel.messages) == 1
