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
from earnings_bot.known import KnownCompany, known_companies, to_target
from earnings_bot.pipeline import Releases
from earnings_bot.recorder import ET, Journal
from earnings_bot.source_audit import Speed
from earnings_bot.sources import PR_NEWSWIRE_EARNINGS
from earnings_bot.track_progress import TrackProgress
from earnings_bot.tracking_commands import OFFLINE, TrackingCommands
from earnings_bot.unattended import DirectoryWatcher
from tests.burst_replay import LocalClock
from tests.fakes import RoutedFetcher
from tests.sec_data import LCID_CIK, LCID_ROUTE, LCID_SUBMISSIONS, sec_routes
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
    events: list[str] | None = None,
    **options: str | None,
) -> str:
    """Runs a command handler as one server member and returns its one reply, sent at once or
    after deferring. `events` records "defer" and "reply" in order."""
    replies: list[str] = []
    events = [] if events is None else events

    async def send_message(content: str, *, ephemeral: bool = False) -> None:
        assert ephemeral, "calendar replies are only for the member who asked"
        assert "defer" not in events, "Discord refuses a second response"
        events.append("reply")
        replies.append(content)

    async def defer(*, ephemeral: bool = False, thinking: bool = False) -> None:
        assert ephemeral and thinking, "only the member who asked sees the bot thinking"
        assert not events, "Discord refuses a second response"
        events.append("defer")

    async def follow_up(content: str, *, ephemeral: bool = False) -> None:
        assert ephemeral, "calendar replies are only for the member who asked"
        assert "defer" in events, "a follow-up needs a deferred response"
        events.append("reply")
        replies.append(content)

    interaction = SimpleNamespace(
        guild_id=guild,
        user=SimpleNamespace(id=user),
        permissions=discord.Permissions(manage_guild=admin),
        response=SimpleNamespace(send_message=send_message, defer=defer),
        followup=SimpleNamespace(send=follow_up),
    )
    await handler(cast(discord.Interaction, interaction), *args, **options)
    [reply] = replies
    return reply


async def never_audit(ticker: str, company: KnownCompany, when: datetime) -> Speed:
    raise AssertionError("these tests don't run the scorecard's source audit")


@pytest.fixture
def watcher(tmp_path: Path) -> Iterator[DirectoryWatcher]:
    """A directory watcher with no schedule files, as the bot builds it, at a fixed time. SEC
    knows Lucid, which isn't in the catalog."""
    fetcher = RoutedFetcher(sec_routes())
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

    assert reply == "✅ Now tracking **NVDA** · NVIDIA\nFollow the research live in <#99>."
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

    assert reply.splitlines() == [
        "👍 Already tracking **NVDA** · NVIDIA",
        describe(sooner, "Scheduled"),
        "Follow the research live in <#99>.",
    ]
    assert tracked(watcher) == ["NVDA"]


class LoggedFetcher(RoutedFetcher):
    """Records each request in `events`, to put it in order with the command's responses."""

    def __init__(self, routes: dict[str, tuple[int, bytes]], events: list[str]) -> None:
        super().__init__(routes)
        self.events = events

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
        self.events.append("request")
        return await super().request(
            method, url, headers, give_up_after, urgent, allow_redirects=allow_redirects
        )


async def test_any_ticker_sec_lists_is_tracked_with_what_sec_says_about_it(
    watcher: DirectoryWatcher, commands: TrackingCommands, tmp_path: Path
) -> None:
    events: list[str] = []
    watcher.issuers.fetcher = LoggedFetcher(sec_routes(), events)

    reply = await call(commands.track, "lcid", events=events)

    assert reply == (
        "✅ Now tracking **LCID** · Lucid Group, Inc.\nFollow the research live in <#99>."
    )
    # Deferred before SEC is asked, since Discord gives up on a reply after 3 seconds
    assert events == ["defer", "request", "request", "reply"]
    [row] = watcher.calendar.entries()
    assert (row.ticker, row.cik) == ("LCID", LCID_CIK)
    assert row.company.names == ["Lucid Group", "NASDAQ: LCID", "NYSE: LCID"]
    assert (row.company.release_time, row.company.fiscal_year_end) == ("16:05", "1231")
    assert watcher.dates.changed.is_set()

    # A repeat uses the saved facts without asking SEC again
    events.clear()
    assert (await call(commands.track, "LCID", user=2, events=events)).startswith(
        "👍 Already tracking **LCID**"
    )
    assert events == ["defer", "reply"]

    # The facts are saved with the calendar, so a restart doesn't need SEC either
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        [restarted] = tracking_calendar.Calendar(store).entries()
    assert restarted == row


@pytest.mark.parametrize(
    ("typed", "refusal"),
    [
        ("zzzz", "ZZZZ isn't on SEC's list of company tickers."),
        ("  ", "Enter a ticker symbol, such as NVDA."),
        ("NV DA", "Enter a ticker symbol, such as NVDA."),
    ],
)
async def test_a_ticker_sec_doesnt_list_is_refused(
    watcher: DirectoryWatcher, commands: TrackingCommands, typed: str, refusal: str
) -> None:
    assert await call(commands.track, typed) == f"⚠️ {refusal}"
    assert tracked(watcher) == []
    assert not watcher.dates.changed.is_set()
    assert watcher.store.db.execute("SELECT count(*) FROM tracking_adds").fetchone()[0] == 0


async def test_when_sec_doesnt_answer_nothing_is_saved_and_a_retry_works(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    routes = sec_routes()
    routes[LCID_ROUTE] = (503, b"")
    watcher.issuers.fetcher = RoutedFetcher(routes)

    reply = await call(commands.track, "LCID")

    assert reply == "⚠️ SEC's company data didn't load. Try again in a minute."
    assert tracked(watcher) == []
    assert watcher.store.db.execute("SELECT count(*) FROM tracking_adds").fetchone()[0] == 0

    routes[LCID_ROUTE] = (200, json.dumps(LCID_SUBMISSIONS).encode())
    assert (await call(commands.track, "LCID")).startswith("✅ Now tracking **LCID**")


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

    assert reply.startswith(f"⚠️ {refusal}")
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
    assert (
        reply == f"📌 {describe(row.target, 'Manual override')}\nFollow the research live in <#99>."
    )

    # Pinning an issuer that's already tracked moves its window
    await call(commands.track, "NVDA", admin=True, when="2026-11-17 16:25", period="FY2027Q3")
    [row] = watcher.calendar.entries()
    assert row.target and row.target.expected == datetime(2026, 11, 17, 16, 25, tzinfo=ET)


async def test_an_admin_can_pin_a_ticker_outside_the_catalog(
    watcher: DirectoryWatcher, commands: TrackingCommands
) -> None:
    reply = await call(
        commands.track, "LCID", admin=True, when="2026-11-12T16:05", period="FY2026Q3"
    )

    [row] = watcher.calendar.entries()
    assert row.target is not None
    assert (
        reply == f"📌 {describe(row.target, 'Manual override')}\nFollow the research live in <#99>."
    )
    # Its wire isn't known, so both are watched, along with EDGAR
    assert row.target.newswire == row.target.prnewswire == row.company.names
    assert [s.name for s in row.target.sources()] == [
        "edgar",
        "edgar_latest",
        "businesswire_busted",
        "prnewswire_busted",
        "prnewswire",
    ]


async def test_the_daily_limit_and_the_calendar_cap_are_explained(
    watcher: DirectoryWatcher, commands: TrackingCommands, monkeypatch: pytest.MonkeyPatch
) -> None:
    for ticker in ("AAPL", "AMD", "AMZN", "META", "MSFT"):
        assert (await call(commands.track, ticker)).startswith(f"✅ Now tracking **{ticker}**")

    reply = await call(commands.track, "NVDA")

    assert reply == "⚠️ You can add up to 5 tickers per Eastern calendar day."
    assert "NVDA" not in tracked(watcher)
    # The limit is per member
    assert (await call(commands.track, "NVDA", user=2)).startswith("✅ Now tracking **NVDA**")

    # Lower the cap rather than track 50 companies
    monkeypatch.setattr(tracking_calendar, "MAX_TRACKED", 6)
    assert await call(commands.track, "TSLA", user=3) == (
        "⚠️ This calendar already tracks 6 companies."
    )
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

    assert await call(commands.untrack, "nvda") == (
        "⚠️ Manage Server is required to remove a ticker."
    )
    assert tracked(watcher) == ["NVDA"]
    assert not watcher.progress.changed.is_set()

    assert await call(commands.untrack, " nvda ", admin=True) == (
        "🗑️ Removed **NVDA** from the calendar."
    )
    assert tracked(watcher) == []
    # So the scorecard is edited to say the issuer was removed
    assert watcher.progress.removed == {NVDA_CIK: "NVDA"}
    assert watcher.progress.changed.is_set()

    assert await call(commands.untrack, "NVDA", admin=True) == "**NVDA** wasn't on the calendar."
    assert await call(commands.untrack, "ZZZZ", admin=True) == "**ZZZZ** wasn't on the calendar."

    # A company from outside the catalog is removed by its ticker, without asking SEC
    await call(commands.track, "LCID")
    fetcher = cast(RoutedFetcher, watcher.issuers.fetcher)
    requests = len(fetcher.requests)
    assert await call(commands.untrack, "lcid", admin=True) == (
        "🗑️ Removed **LCID** from the calendar."
    )
    assert tracked(watcher) == []
    assert watcher.progress.removed == {NVDA_CIK: "NVDA", LCID_CIK: "LCID"}
    assert len(fetcher.requests) == requests


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
    assert await call(commands.upcoming) == "📅 No earnings windows in the next 7 days."

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
        "📅 **Earnings in the next 7 days**",
        describe(found, evidence),
        describe(nvda, "Schedule file"),
        "-# Still looking for dates: CRWD",
    ]
    # The evidence's link is a short masked one
    assert (
        "Issuer confirmed release time · [source](<https://ir.homedepot.com/news-releases/q3>)"
        in reply
    )


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
    assert lines[0] == "📅 **Earnings in the next 7 days**"
    assert lines[-1] == "-# …and more after these."
    assert lines[1:-1] == [describe(t, "Schedule file") for t in targets[: len(lines) - 2]]


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
        assert "**$PLTR Q4 FY26 earnings are out**" in alert.content
        assert "https://investors.palantir.com/news-details/2027/palantir-reports-q4-2026" in (
            alert.content
        )
        [report] = [line for line in status if "PLTR Q4 FY26 window closed" in line]
        assert report.splitlines()[1].startswith(
            "🥇 IR site press releases and files (Q4 feed) at "
        )
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


LUCID_RELEASE = "https://www.prnewswire.com/news-releases/lucid-q3-2026-results-302600001.html"


def _wire_item(title: str, lead: str, link: str) -> str:
    return (
        f"<item><title>{title}</title><link>{link}</link><guid>{link}</guid>"
        f"<description>{lead}</description></item>"
    )


class WireDay(RoutedFetcher):
    """SEC, and PR Newswire's earnings feed. Its server lists Lucid's results from `released_at`
    (time.monotonic()) on, but its CDN's copy at the usual URL still predates them, as on
    Nov 9. Another company's results are listed throughout."""

    def __init__(self) -> None:
        super().__init__(sec_routes())
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
        items = [
            _wire_item(
                "Acme Reports Third Quarter 2026 Results",
                "AUSTIN, Texas /PRNewswire/ -- Acme Corp (NYSE: ACME), a Lucid supplier",
                "https://www.prnewswire.com/news-releases/acme-q3-302600000.html",
            )
        ]
        fresh = url != PR_NEWSWIRE_EARNINGS
        if fresh and time.monotonic() >= self.released_at:
            items.insert(
                0,
                _wire_item(
                    "Lucid Announces Third Quarter 2026 Results",
                    "NEWARK, Calif., Nov. 9, 2026 /PRNewswire/ -- Lucid (Nasdaq: LCID), maker",
                    LUCID_RELEASE,
                ),
            )
        if url.lower() == PR_NEWSWIRE_EARNINGS.lower():
            self.requests.append((method, url, headers))
            feed = '<rss version="2.0"><channel>' + "".join(items) + "</channel></rss>"
            return Response(url, 200, {}, feed.encode(), 0.01)
        return await super().request(
            method, url, headers, give_up_after, urgent, allow_redirects=allow_redirects
        )


async def test_a_ticker_outside_the_catalog_gets_its_alert_from_the_wire(
    tmp_path: Path,
) -> None:
    clock = ShiftedClock()
    clock.sync()
    journal = Journal(io.StringIO(), clock)
    fetcher = WireDay()
    channel = FakeDiscord()
    status: list[str] = []
    with DeliveryStore(tmp_path / "state.db", 7) as store:
        releases = Releases(
            FakePoster, fetcher, journal, None, None, lambda t: channel.poster(store, t)
        )
        bot = DirectoryWatcher(tmp_path, fetcher, journal, releases, store, status.append)
        bot.guild_id = GUILD
        # Soon enough that the window, which opens 10 minutes early, is already open
        when = (clock.now() + timedelta(minutes=5)).astimezone(ET)
        reply = await call(
            TrackingCommands(lambda: bot).track,
            "LCID",
            admin=True,
            when=f"{when:%Y-%m-%dT%H:%M}",
            period="FY2026Q3",
        )
        assert "Manual override" in reply
        [row] = bot.calendar.entries()
        assert row.target is not None

        # PR Newswire is polled every 2 seconds. Skip to 6 seconds before the window closes, with
        # the results out a second in, after the first poll has seen the list without them.
        clock.shift = row.target.window[1] - clock.now() - timedelta(seconds=6)
        fetcher.released_at = time.monotonic() + 1
        await bot.scan()
        await asyncio.gather(*(window.task for window in bot.windows.values()))

        [alert] = channel.messages.values()
        assert "**$LCID Q3 FY26 earnings are out**" in alert.content
        assert LUCID_RELEASE in alert.content
        [report] = [line for line in status if "LCID Q3 FY26 window closed" in line]
        # PR Newswire's own server had it while its CDN's copy didn't
        assert report.splitlines()[1].startswith("🥇 PR Newswire earnings feed at ")
