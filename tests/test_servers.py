"""Several Discord servers: each tracks its own companies, a company several track is watched
once, and its alert goes to each of them at once."""

import asyncio
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import discord
import pytest

from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.pipeline import FanOutPoster, Releases, fan_out
from earnings_bot.recorder import ET, Journal, Target
from earnings_bot.release_cards import release_content
from earnings_bot.tracking_commands import NOT_CONNECTED, TrackingCommands
from earnings_bot.unattended import DirectoryWatcher, Server, _Window, durable_posters
from tests.builders import GUILD
from tests.clocks import ShiftedClock
from tests.fakes import (
    FakePoster,
    PolledReleaseDay,
    ReleaseDay,
    WatchedDiscord,
    call,
    quick_windows,
)

OTHER_GUILD = 777


def test_other_servers_alert_channels_come_from_one_comma_separated_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "111")
    monkeypatch.setenv("DISCORD_SERVER_CHANNEL_IDS", "222, 333,222,111")
    settings = Settings.model_validate({"discord_token": "test"})
    assert settings.alert_channel_ids == [111, 222, 333]
    monkeypatch.delenv("DISCORD_SERVER_CHANNEL_IDS")
    assert Settings.model_validate({"discord_token": "test"}).alert_channel_ids == [111]


class Refusing(FakePoster):
    async def send(self, content: str) -> datetime | None:
        await asyncio.sleep(0)
        raise discord.DiscordException("refused")


async def test_every_server_gets_each_step_and_one_refusal_doesnt_stop_the_others() -> None:
    first, second = FakePoster(), FakePoster()
    poster = fan_out([first, second])
    assert isinstance(poster, FanOutPoster)
    poster.first_seen = datetime(2026, 11, 17, 7, tzinfo=ET)

    await poster.send("🚨 **$KLAR Q3 FY26 earnings are out**")
    await poster.set_summary("Revenue $1.2B", "Beta · Summary by Muse Ember in 4s")

    assert [kind for kind, _ in first.calls] == [kind for kind, _ in second.calls]
    assert [kind for kind, _ in first.calls] == ["send", "summary"]
    assert first.first_seen == second.first_seen == poster.first_seen
    # One server's channel refusing doesn't keep the alert from the other, and the refusal
    # still comes back so the step is tried again
    working = FakePoster()
    with pytest.raises(discord.DiscordException):
        await fan_out([Refusing(), working]).send("alert")
    assert [kind for kind, _ in working.calls] == ["send"]
    # One server: its poster as it is
    alone = FakePoster()
    assert fan_out([alone]) is alone


async def test_a_company_other_servers_track_is_watched_once_and_alerted_in_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quick_windows(monkeypatch)
    clock = ShiftedClock()
    clock.sync()
    journal = Journal(io.StringIO(), clock)
    fetcher = PolledReleaseDay()
    channels = [WatchedDiscord() for _ in range(4)]
    guilds = [GUILD, OTHER_GUILD, 888, 999]
    with (
        DeliveryStore(tmp_path / "state.db", 7) as mine,
        DeliveryStore(tmp_path / "state.db", 8) as second,
        DeliveryStore(tmp_path / "state.db", 9) as third,
        DeliveryStore(tmp_path / "state.db", 10) as fourth,
    ):
        stores = [mine, second, third, fourth]
        servers = [
            Server(store, cast(discord.abc.Messageable, channel), guild)
            for store, channel, guild in zip(stores, channels, guilds, strict=True)
        ]
        releases = Releases(FakePoster, fetcher, journal, None)
        bot = DirectoryWatcher(
            tmp_path, fetcher, journal, releases, mine, lambda _: None, servers=servers
        )
        releases.durable_poster = durable_posters(bot, 42)
        commands = TrackingCommands(lambda: bot)
        when = (clock.now() + timedelta(minutes=5)).astimezone(ET)
        # The second and third servers track Palantir; the deployment's own and the fourth don't
        for guild in guilds[1:3]:
            reply = await call(
                commands.track,
                "PLTR",
                admin=True,
                guild=guild,
                when=f"{when:%Y-%m-%dT%H:%M}",
                period="FY2026Q4",
            )
            assert "Manual override" in reply
        assert [[r.ticker for r in s.calendar.entries()] for s in servers] == [
            [],
            ["PLTR"],
            ["PLTR"],
            [],
        ]
        [row] = servers[1].calendar.entries()
        assert row.target is not None

        await bot.scan()
        assert len(bot.windows) == 1  # one window, however many servers track it
        # The results come out after the first poll; once both servers have their alert, skip
        # to the end of the window
        for channel in channels[1:3]:
            await asyncio.wait_for(channel.sent.wait(), 10)
        clock.shift = row.target.window[1] - clock.now() + timedelta(seconds=1)
        await asyncio.gather(*(window.task for window in bot.windows.values()))

        for channel in channels[1:3]:
            [alert] = channel.messages.values()
            assert "**$PLTR Q4 FY26 earnings are out**" in alert.content
            assert "palantir-reports-q4-2026" in release_content(alert)
        assert channels[0].messages == channels[3].messages == {}
        # Each server keeps its own record of its own alert
        assert [s.get(row.target.release_key) is not None for s in stores] == [
            False,
            True,
            True,
            False,
        ]


async def test_a_command_works_with_the_list_of_the_server_its_used_in(tmp_path: Path) -> None:
    journal = Journal(io.StringIO())
    fetcher = ReleaseDay()
    with (
        DeliveryStore(tmp_path / "state.db", 7) as store,
        DeliveryStore(tmp_path / "state.db", 8) as their_store,
    ):
        servers = [Server(store, None, GUILD), Server(their_store, None, OTHER_GUILD)]
        releases = Releases(FakePoster, fetcher, journal, None)
        bot = DirectoryWatcher(
            tmp_path, fetcher, journal, releases, store, lambda _: None, servers=servers
        )
        commands = TrackingCommands(lambda: bot)

        reply = await call(commands.track, "NVDA", guild=OTHER_GUILD)

        assert "Now tracking **NVDA**" in reply
        assert [r.ticker for r in servers[1].calendar.entries()] == ["NVDA"]
        assert servers[0].calendar.entries() == []
        # A shared scheduler's populated plan must not appear as another server's schedule.
        [tracked] = servers[1].calendar.entries()
        target = Target(ticker="NVDA", cik=tracked.cik, expected=journal.now() + timedelta(days=1))
        bot.planned = {target.release_key: target}
        assert not bot.desk(GUILD).planned  # type: ignore[union-attr]
        assert bot.desk(OTHER_GUILD).planned == bot.planned  # type: ignore[union-attr]
        assert "NVDA" not in await call(commands.upcoming, guild=GUILD)

        async def idle() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(idle())
        try:
            bot.windows = {target.release_key: _Window(target, task)}
            assert "NVDA" not in str(bot.health_embed("test", None, servers[0]).to_dict())
            assert "NVDA" in str(bot.health_embed("test", None, servers[1]).to_dict())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A server the bot doesn't post to has no list
        assert await call(commands.track, "NVDA", guild=999) == NOT_CONNECTED
        assert await call(commands.upcoming, guild=999) == NOT_CONNECTED


def test_a_schedule_files_release_goes_to_the_deployments_own_server(tmp_path: Path) -> None:
    journal = Journal(io.StringIO())
    with (
        DeliveryStore(tmp_path / "state.db", 7) as store,
        DeliveryStore(tmp_path / "state.db", 8) as their_store,
    ):
        servers = [Server(store, None, GUILD), Server(their_store, None, OTHER_GUILD)]
        bot = DirectoryWatcher(
            tmp_path,
            ReleaseDay(),
            journal,
            Releases(FakePoster, ReleaseDay(), journal, None),
            store,
            lambda _: None,
            servers=servers,
        )
        hd = Target(ticker="HD", cik=354950, expected=datetime(2026, 11, 17, 6, tzinfo=ET))
        assert bot.subscribers(hd) == servers[:1]
        # Once another server tracks it, the release is theirs to get
        servers[1].calendar.add("HD")
        assert bot.subscribers(hd) == servers[1:]


async def test_the_watcher_gives_each_configured_server_its_own_list_and_scorecards(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from earnings_bot.calendar import Calendar
    from earnings_bot.unattended import watch_directory

    # The deployment's own server already tracks NVIDIA; the other server is new
    with DeliveryStore(tmp_path / ".earnings-state.sqlite3", 7) as before:
        Calendar(before).add("NVDA")
    mine = SimpleNamespace(id=7, guild=SimpleNamespace(id=GUILD), send=None)
    theirs = SimpleNamespace(id=8, guild=SimpleNamespace(id=OTHER_GUILD), send=None)
    started: list[DirectoryWatcher] = []
    ready = asyncio.Event()

    def on_ready(watcher: DirectoryWatcher) -> None:
        started.append(watcher)
        ready.set()

    task = asyncio.create_task(
        watch_directory(
            tmp_path,
            cast(discord.abc.Messageable, mine),
            7,
            42,
            "Test Person test@example.com",
            None,
            None,
            None,
            on_ready,
            other_channels=[cast(discord.TextChannel, theirs)],
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 10)
        [watcher] = started
        assert [(s.channel_id, s.guild_id) for s in watcher.servers] == [
            (7, GUILD),
            (8, OTHER_GUILD),
        ]
        # Each has its own research and scorecards, the scorecards in its own channel
        assert all(s.dates and s.progress for s in watcher.servers)
        assert watcher.servers[1].progress is not watcher.servers[0].progress
        assert watcher.desk(OTHER_GUILD) is not None
        # A new server starts with an empty list; it doesn't take the deployment's
        assert [r.ticker for r in watcher.servers[0].calendar.entries()] == ["NVDA"]
        assert watcher.servers[1].calendar.entries() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_bot_registers_its_commands_in_every_configured_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from earnings_bot.bot import EarningsBot

    monkeypatch.setenv("DISCORD_CHANNEL_ID", "7")
    monkeypatch.setenv("DISCORD_SERVER_CHANNEL_IDS", "8,9")
    monkeypatch.setenv("DISCORD_GUILD_ID", str(GUILD))
    channels = {
        8: Mock(spec=discord.TextChannel, id=8, guild=Mock(id=OTHER_GUILD)),
        9: Mock(spec=discord.VoiceChannel, id=9, guild=Mock(id=888)),  # takes no messages
    }
    bot = EarningsBot(Settings.model_validate({"discord_token": "test"}))
    monkeypatch.setattr(bot, "fetch_channel", AsyncMock(side_effect=channels.__getitem__))
    synced: list[int] = []

    async def sync(*, guild: discord.abc.Snowflake | None = None) -> list[object]:
        synced.append(guild.id if guild else 0)
        return []

    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    assert synced == [GUILD, OTHER_GUILD, 888]
    assert [channel.id for channel in await bot._other_channels()] == [8]


def test_the_shared_calendar_reads_each_company_once_and_writes_to_every_server(
    tmp_path: Path,
) -> None:
    from earnings_bot.calendar import Calendar, SharedCalendar

    with (
        DeliveryStore(tmp_path / "state.db", 7) as mine,
        DeliveryStore(tmp_path / "state.db", 8) as theirs,
    ):
        own, other = Calendar(mine), Calendar(theirs)
        own.add("NVDA")
        other.add("NVDA")
        other.add("AMZN")
        shared = SharedCalendar(lambda: [own, other])
        assert [row.ticker for row in shared.entries()] == ["AMZN", "NVDA"]

        nvda = Target(
            ticker="NVDA", cik=1045810, expected=datetime(2026, 11, 17, 16, 20, tzinfo=ET)
        )
        shared.set_target(nvda, "Issuer confirmed release time")
        shared.save_review(1045810, datetime(2026, 11, 15, tzinfo=ET), "{}")

        for calendar in (own, other):
            row = calendar.find("NVDA")
            assert row and row.target == nvda and row.evidence == "Issuer confirmed release time"
            assert calendar.review(1045810)[1] == "{}"
        # Written only where the company is tracked
        assert own.find("AMZN") is None
        with pytest.raises(TypeError):
            shared.add("MSFT")


async def test_a_company_two_servers_track_is_researched_once_for_both(tmp_path: Path) -> None:
    from earnings_bot.calendar import SharedCalendar

    journal = Journal(io.StringIO())
    with (
        DeliveryStore(tmp_path / "state.db", 7) as mine,
        DeliveryStore(tmp_path / "state.db", 8) as theirs,
    ):
        servers = [Server(mine, None, GUILD), Server(theirs, None, OTHER_GUILD)]
        bot = DirectoryWatcher(
            tmp_path,
            ReleaseDay(),
            journal,
            Releases(FakePoster, ReleaseDay(), journal, None),
            mine,
            lambda _: None,
            servers=servers,
        )
        # One research loop, over both servers' companies
        assert all(server.dates is bot.dates for server in servers)
        assert isinstance(bot.dates.calendar, SharedCalendar)
        from earnings_bot.investigate import INVESTIGATION
        from earnings_bot.known import SourceCheck, known_companies, to_target

        now = datetime(2026, 11, 14, 21, tzinfo=ET)
        company = known_companies()["NVDA"].model_copy(
            update={
                "checks": [SourceCheck(source="SEC 8-K filings", watch=True, why="Filed here")],
                "investigation": INVESTIGATION,
                "investigated": now.date(),
            }
        )
        servers[0].calendar.add("NVDA", company)
        servers[1].calendar.add("NVDA", company)
        assert [row.ticker for row in bot.dates.calendar.entries()] == ["NVDA"]
        # What the deployment's own server already found
        found = to_target("NVDA", datetime(2026, 11, 17, 16, 20, tzinfo=ET), 2027, 3, company)
        servers[0].calendar.set_target(found, "Issuer's earnings event")
        bot.dates.now = lambda: now
        [row] = bot.dates.calendar.entries()

        await bot.dates.run_once(row)

        # The one check wrote what it found to both servers
        for server in servers:
            [entry] = server.calendar.entries()
            assert entry.target is not None and entry.target.expected == found.expected


@pytest.mark.parametrize("fails", [False, True])
async def test_each_companys_speed_audit_runs_once_for_every_server(
    tmp_path: Path, fails: bool
) -> None:
    from dataclasses import replace

    from earnings_bot.track_progress import TrackProgress
    from tests.builders import INVESTIGATED, SPEED
    from tests.builders import PROGRESS_NOW as NOW
    from tests.fakes import WatchedDiscord

    audits: list[str] = []
    done = asyncio.Event()
    speed = replace(SPEED, history=[datetime(2026, 8, 5, 16, 5, tzinfo=ET)])

    async def audit(ticker: str, company: object, when: datetime) -> object:
        audits.append(ticker)
        await asyncio.sleep(0)
        done.set()
        if fails:
            raise OSError("temporary source failure")
        return speed

    journal = Journal(io.StringIO())
    channels = [WatchedDiscord(), WatchedDiscord()]
    with (
        DeliveryStore(tmp_path / "state.db", 7) as mine,
        DeliveryStore(tmp_path / "state.db", 8) as theirs,
        DeliveryStore(tmp_path / "cards.db", 7) as my_cards,
        DeliveryStore(tmp_path / "cards.db", 8) as their_cards,
    ):
        servers = [Server(mine, None, GUILD), Server(theirs, None, OTHER_GUILD)]
        bot = DirectoryWatcher(
            tmp_path,
            ReleaseDay(),
            journal,
            Releases(FakePoster, ReleaseDay(), journal, None),
            mine,
            lambda _: None,
            servers=servers,
        )
        for server in servers:
            server.calendar.add("LCID", INVESTIGATED)
        trackers = [
            TrackProgress(
                server.calendar,
                cards,
                cast(discord.abc.Messageable, channel),
                42,
                planned=dict,
                now=lambda: NOW,
                busy=lambda: False,
                audit=audit,  # type: ignore[arg-type]
                card_spacing=0.0,
                reviews=bot.shared,
                claim=bot.claim_audit,
                release=bot.auditing.discard,
                next_attempt=bot.audit_next_attempts,
            )
            for server, cards, channel in zip(
                servers, (my_cards, their_cards), channels, strict=True
            )
        ]
        tasks = [asyncio.create_task(tracker.run()) for tracker in trackers]
        try:
            await asyncio.wait_for(done.wait(), 5)
            for tracker in trackers:
                tracker.changed.set()
            for channel in channels:
                await asyncio.wait_for(channel.sent.wait(), 5)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        assert audits == ["LCID"]  # once, not once per server
        assert bot.auditing == set()
        assert bot.audit_next_attempts[INVESTIGATED.cik] > NOW
        for server in servers:
            assert (server.calendar.review(INVESTIGATED.cik)[1] is None) == fails
        # Each server still gets its own scorecard
        assert all(channel.messages for channel in channels)


EVIDENCE_PAGE = "https://ir.lucidmotors.com/news-releases/lucid-to-report-third-quarter"


async def borrowing(
    tmp_path: Path, page_status: int, audited_ago: timedelta, *, linked: bool = True
) -> tuple[str, Server, Server, list[str]]:
    """Server A tracks Lucid, a company outside the catalog, with its research done; server B
    then tracks it. Returns B's reply, both servers and the URLs requested. Looking Lucid up at
    SEC would fail here: every other URL answers 404."""
    from dataclasses import replace

    from earnings_bot.known import to_target
    from tests.builders import INVESTIGATED, SPEED
    from tests.fakes import RoutedFetcher

    fetcher = RoutedFetcher({EVIDENCE_PAGE: (page_status, b"<html>Lucid call</html>")})
    journal = Journal(io.StringIO())
    now = journal.now()
    mine = DeliveryStore(tmp_path / "state.db", 7)
    theirs = DeliveryStore(tmp_path / "state.db", 8)
    servers = [Server(mine, None, GUILD), Server(theirs, None, OTHER_GUILD)]
    bot = DirectoryWatcher(
        tmp_path,
        fetcher,
        journal,
        Releases(FakePoster, fetcher, journal, None),
        mine,
        lambda _: None,
        servers=servers,
    )
    company = INVESTIGATED.model_copy(update={"investigated": now.date()})
    servers[0].calendar.add("LCID", company)
    found = to_target("LCID", now + timedelta(days=2), 2026, 3, company)
    servers[0].calendar.set_target(
        found, f"Issuer confirmed release time <{EVIDENCE_PAGE}>" if linked else "Manual override"
    )
    speed = replace(SPEED, history=[datetime(2026, 8, 5, 16, 5, tzinfo=ET)])
    servers[0].calendar.save_review(found.cik, now - audited_ago, speed.to_json())

    reply = await call(TrackingCommands(lambda: bot).track, "LCID", guild=OTHER_GUILD)

    return reply, servers[0], servers[1], [url for _, url, _ in fetcher.requests]


async def test_a_server_takes_research_another_did_after_a_quick_check(tmp_path: Path) -> None:
    reply, mine, theirs, requested = await borrowing(tmp_path, 200, timedelta(hours=2))

    assert "📥 Took another server's research: its release date and its speed audit." in reply
    [own] = mine.calendar.entries()
    [taken] = theirs.calendar.entries()
    assert taken.target == own.target and taken.evidence == own.evidence
    assert taken.company == own.company  # the investigated profile came along
    assert theirs.calendar.review(own.cik) == mine.calendar.review(own.cik)
    # The one check, that the date's source still answers; no SEC lookup or investigation
    assert requested == [EVIDENCE_PAGE]
    for server in (mine, theirs):
        server.store.db.close()


@pytest.mark.parametrize("status", [403, 404, 410, 429, 500, 503])
async def test_what_fails_the_check_is_researched_again(tmp_path: Path, status: int) -> None:
    reply, mine, theirs, _ = await borrowing(tmp_path, status, timedelta(days=2))

    assert "🔁 Researching its release date, whose source didn't answer." in reply
    assert "📥" not in reply  # the two-day-old audit didn't come either
    [taken] = theirs.calendar.entries()
    assert taken.target is None
    assert theirs.calendar.review(taken.cik) == (None, None)
    for server in (mine, theirs):
        server.store.db.close()


async def test_manual_dates_are_not_borrowed_and_future_audits_are_not_current(
    tmp_path: Path,
) -> None:
    reply, mine, theirs, requested = await borrowing(
        tmp_path, 200, timedelta(hours=-1), linked=False
    )
    assert "📥" not in reply
    [taken] = theirs.calendar.entries()
    assert taken.target is None
    assert theirs.calendar.review(taken.cik) == (None, None)
    assert requested == []
    for server in (mine, theirs):
        server.store.db.close()


async def test_primary_server_commands_are_registered_without_a_test_guild_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from earnings_bot.bot import EarningsBot

    settings = Settings.model_validate(
        {
            "discord_token": "test",
            "discord_channel_id": 7,
            "discord_guild_id": None,
            "discord_server_channel_ids": [8],
        }
    )
    bot = EarningsBot(settings)
    channels = {
        7: Mock(spec=discord.TextChannel, guild=Mock(id=GUILD)),
        8: Mock(spec=discord.TextChannel, guild=Mock(id=OTHER_GUILD)),
    }
    monkeypatch.setattr(bot, "fetch_channel", AsyncMock(side_effect=channels.__getitem__))
    synced: list[int] = []

    async def sync(*, guild: discord.abc.Snowflake | None = None) -> list[object]:
        synced.append(guild.id if guild else 0)
        return []

    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    assert synced == [GUILD, OTHER_GUILD]


async def test_debug_managers_are_scoped_to_configured_servers_and_owner_stays_global(
    tmp_path: Path,
) -> None:
    from tests.builders import OWNER
    from tests.fakes import client

    fetcher = ReleaseDay()
    journal = Journal(io.StringIO())
    with (
        DeliveryStore(tmp_path / "state.db", 7) as first,
        DeliveryStore(tmp_path / "state.db", 8) as other,
    ):
        servers = [Server(first, None, GUILD), Server(other, None, OTHER_GUILD)]
        watcher = DirectoryWatcher(
            tmp_path,
            fetcher,
            journal,
            Releases(FakePoster, fetcher, journal, None),
            first,
            lambda _: None,
            servers=servers,
        )
        watcher.research_paused = lambda: True
        commands = TrackingCommands(lambda: watcher)
        pause = "⏳ A release is due in the next few minutes. Run the test after it."
        assert (
            await call(
                commands.track, "NVDA", debug=True, admin=True, guild=OTHER_GUILD, app=client()
            )
            == pause
        )
        assert (
            await call(commands.track, "NVDA", debug=True, user=OWNER, guild=9999, app=client())
            == pause
        )
