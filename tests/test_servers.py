"""Several Discord servers: each tracks its own companies, a company several track is watched
once, and its alert goes to each of them at once."""

import asyncio
import io
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import discord
import pytest

from earnings_bot import recorder
from earnings_bot.config import Settings
from earnings_bot.delivery import DeliveryStore
from earnings_bot.pipeline import FanOutPoster, Releases, fan_out
from earnings_bot.recorder import ET, Journal, Target
from earnings_bot.release_cards import release_content
from earnings_bot.tracking_commands import NOT_CONNECTED, TrackingCommands
from earnings_bot.unattended import DirectoryWatcher, Server, durable_posters
from tests.test_pipeline import FakePoster
from tests.test_tracking_commands import GUILD, ReleaseDay, ShiftedClock, call
from tests.test_unattended import FakeDiscord

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
    monkeypatch.setattr(recorder, "pace", lambda at, times, own: (own, None))
    clock = ShiftedClock()
    clock.sync()
    journal = Journal(io.StringIO(), clock)
    fetcher = ReleaseDay()
    channels = [FakeDiscord() for _ in range(4)]
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

        clock.shift = row.target.window[1] - clock.now() - timedelta(seconds=3)
        fetcher.released_at = time.monotonic() + 0.8
        await bot.scan()
        assert len(bot.windows) == 1  # one window, however many servers track it
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
        assert "NVDA" not in await call(commands.upcoming, guild=GUILD)
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

    from earnings_bot.unattended import watch_directory

    from earnings_bot.calendar import Calendar

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
