import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from earnings_bot.browser import PROFILE_PREFIX, leftover_chrome, sweep_leftover_chrome

LEFT = PROFILE_PREFIX + "v9z3ozn7"  # from a bot stopped on Nov 13
LIVE = PROFILE_PREFIX + "zv253xe1"  # this bot's


def chrome(pid: int, parent: int, profile: str, kind: str = "") -> dict[str, Any]:
    command = f'"chrome.exe" --user-data-dir=C:\\Temp\\{profile} {kind}'.strip()
    return {
        "ProcessId": pid,
        "ParentProcessId": parent,
        "Name": "chrome.exe",
        "CommandLine": command,
    }


PROCESSES = [
    {"ProcessId": 100, "ParentProcessId": 1, "Name": "python.exe", "CommandLine": "earnings-bot"},
    # Left by a bot that's gone: its main process's parent, 35032, isn't running
    chrome(17696, 35032, LEFT),
    chrome(17700, 17696, LEFT, "--type=renderer"),
    # A profile whose main process is gone too, with a helper still running
    chrome(17800, 17000, PROFILE_PREFIX + "v8flh0fy", "--type=utility"),
    # This bot's Chrome and its helper
    chrome(200, 100, LIVE),
    chrome(201, 200, LIVE, "--type=gpu-process"),
    # The person's own Chrome, which is never touched
    {"ProcessId": 300, "ParentProcessId": 1, "Name": "chrome.exe", "CommandLine": "chrome.exe"},
]


def test_chrome_a_gone_bot_left_is_found_and_a_live_ones_isnt() -> None:
    left, used = leftover_chrome(PROCESSES)
    assert left == {17696, 17700, 17800}
    assert used == {LIVE}


def test_the_sweep_ends_leftovers_and_removes_profiles_no_live_chrome_uses(tmp_path: Path) -> None:
    for name in (LEFT, LIVE, PROFILE_PREFIX + "old00001", "unrelated-folder"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "Local State").write_text("{}", encoding="utf-8")
    ended: list[int] = []

    result = sweep_leftover_chrome(lambda: PROCESSES, ended.append, tmp_path, settle=0)

    assert sorted(ended) == [17696, 17700, 17800]
    assert result == (3, 2)
    assert sorted(path.name for path in tmp_path.iterdir()) == [LIVE, "unrelated-folder"]


def test_with_nothing_left_the_sweep_ends_nothing(tmp_path: Path) -> None:
    (tmp_path / LIVE).mkdir()
    ended: list[int] = []
    live = [p for p in PROCESSES if LIVE in str(p["CommandLine"]) or p["Name"] == "python.exe"]
    assert sweep_leftover_chrome(lambda: live, ended.append, tmp_path, settle=0) == (0, 0)
    assert ended == [] and (tmp_path / LIVE).exists()


_OWNER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from earnings_bot._windows_desktop import DesktopProcess
    child = DesktopProcess(Path(sys.executable), ["-c", "import time; time.sleep(120)"])
    print(child.pid, child.ended_with_owner, flush=True)
    time.sleep(120)
    """
)


def _running(pid: int) -> bool:
    found = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
    )
    return str(pid) in found.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Chrome's hidden desktop is Windows only")
def test_chrome_ends_when_the_bot_that_started_it_is_killed() -> None:
    """A deploy stops the bot by ending its process; Chrome used to keep running."""
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        line = owner.stdout.readline() if owner.stdout else ""
        if not line:
            pytest.skip(f"no hidden desktop here: {owner.stderr.read() if owner.stderr else ''}")
        pid, ended_with_owner = line.split()
        assert ended_with_owner == "True"
        assert _running(int(pid))
    finally:
        owner.kill()  # TerminateProcess, as a stopped task does
        owner.wait(10)
    deadline = time.monotonic() + 10
    while _running(int(pid)) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _running(int(pid))


async def test_the_bot_sweeps_leftover_chrome_before_it_starts_watching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from earnings_bot import bot as bot_module
    from earnings_bot.bot import EarningsBot
    from earnings_bot.config import Settings

    order: list[str] = []
    monkeypatch.setattr(
        bot_module, "sweep_leftover_chrome", lambda: order.append("sweep") or (11, 18)
    )
    earnings = EarningsBot(Settings.model_validate({"discord_token": "test"}), tmp_path)
    monkeypatch.setattr(earnings.tree, "sync", AsyncMock(return_value=[]))

    async def watch(schedule: Path) -> None:
        order.append("watch")

    monkeypatch.setattr(earnings, "_watch", watch)

    await earnings.setup_hook()
    assert earnings.watcher is not None
    await earnings.watcher

    assert order == ["sweep", "watch"]
