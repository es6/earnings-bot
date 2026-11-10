import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from earnings_bot import __main__ as entry


def test_alerts_and_status_can_share_one_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything goes to #bot-testing in HQ Nest
    monkeypatch.chdir(tmp_path)  # away from a developer's real .env
    for name, value in {
        "DISCORD_TOKEN": "not-a-real-token",
        "DISCORD_CHANNEL_ID": "5",
        "DISCORD_STATUS_CHANNEL_ID": "5",
        "SEC_USER_AGENT": "Jane Doe jane@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["earnings-bot", str(tmp_path)])
    monkeypatch.setattr(entry, "configure_console", lambda: None)
    monkeypatch.setattr(discord.utils, "setup_logging", lambda **kwargs: None)
    started: list[object] = []
    bot = SimpleNamespace(run=lambda token, **_: started.append(token))
    monkeypatch.setattr(entry, "EarningsBot", lambda *args: bot)

    entry.main()

    assert started == ["not-a-real-token"]
