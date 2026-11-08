import sys
from pathlib import Path

import discord
import pytest

from earnings_bot import __main__ as entry


def test_the_status_channel_must_differ_from_the_release_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(entry, "EarningsBot", lambda *args: started.append(args))

    with pytest.raises(SystemExit, match="must differ"):
        entry.main()
    assert not started
