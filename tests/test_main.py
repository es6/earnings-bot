import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from earnings_bot import __main__ as entry


@pytest.fixture
def started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[object, dict[str, object]]]:
    """A launch with a token and a schedule directory, whose bot only records how it was run."""
    monkeypatch.chdir(tmp_path)  # away from a developer's real .env
    for name in ("DISCORD_CHANNEL_ID", "DISCORD_STATUS_CHANNEL_ID", "SEC_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setattr(sys, "argv", ["earnings-bot", str(tmp_path)])
    monkeypatch.setattr(entry, "configure_console", lambda: None)
    monkeypatch.setattr(discord.utils, "setup_logging", lambda **kwargs: None)
    runs: list[tuple[object, dict[str, object]]] = []
    bot = SimpleNamespace(run=lambda token, **options: runs.append((token, options)))
    monkeypatch.setattr(entry, "EarningsBot", lambda *args: bot)
    return runs


def test_the_bot_starts_with_its_token_and_leaves_logging_to_the_entry_point(
    started: list[tuple[object, dict[str, object]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "5")
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")

    entry.main()

    # log_handler=None: discord.py must not install its own handler over ours
    assert started == [("not-a-real-token", {"log_handler": None})]


def test_a_schedule_needs_the_alert_channel_and_the_sec_contact(
    started: list[tuple[object, dict[str, object]]],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        entry.main()

    assert (
        stopped.value.code
        == "To watch a schedule, set DISCORD_CHANNEL_ID and SEC_USER_AGENT in .env"
    )
    assert started == []  # the bot never started


def test_a_schedule_file_that_cant_be_read_stops_the_bot_before_it_starts(
    started: list[tuple[object, dict[str, object]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "5")
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")
    broken = tmp_path / "today.toml"
    broken.write_text("this is = = not toml", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["earnings-bot", str(broken)])

    with pytest.raises(SystemExit) as stopped:
        entry.main()

    assert str(stopped.value.code).startswith(f"Can't read {broken}: ")
    assert started == []


def test_the_bot_says_when_live_summaries_are_off(
    started: list[tuple[object, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "5")
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe jane@example.com")
    monkeypatch.setenv("LIVE_SUMMARIES", "false")

    with caplog.at_level(logging.INFO, logger="earnings_bot"):
        entry.main()

    assert "Live summaries are off" in caplog.text
