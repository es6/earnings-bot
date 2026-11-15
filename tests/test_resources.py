import asyncio
import ctypes
import io
import json
import sys
from pathlib import Path

import pytest

from earnings_bot import recorder
from earnings_bot.recorder import Journal, Target
from earnings_bot.resources import MB, snapshot
from tests.test_recorder import FakeFetcher


class Chrome:
    def __init__(self, used: tuple[int, float, int] | None) -> None:
        self.used = used

    def usage(self) -> tuple[int, float, int] | None:
        return self.used


def test_a_snapshot_adds_up_every_chrome_and_says_what_the_pc_has_left() -> None:
    record = snapshot([Chrome((12, 3.3, 423 * MB)), Chrome((4, 1.0, 100 * MB)), Chrome(None)])
    assert (record["chrome_processes"], record["chrome_cpu_s"], record["chrome_peak_mb"]) == (
        16,
        4.3,
        523,
    )
    assert record["bot_cpu_s"] >= 0
    if sys.platform == "win32":
        assert record["bot_mb"] > 0 and record["free_mb"] > 0


async def test_a_window_records_the_bots_use_of_the_pc(monkeypatch: pytest.MonkeyPatch) -> None:
    async def synced(journal: Journal, periodic: bool = False) -> bool:
        return True

    monkeypatch.setattr(recorder, "sync_clock", synced)
    log = io.StringIO()
    journal = Journal(log)
    target = Target(
        ticker="HD", cik=354950, expected=journal.now(), before_minutes=1, after_minutes=0.003
    )

    other = target.model_copy(update={"ticker": "KLAR", "cik": 2003292})
    fetcher = FakeFetcher((200, ""))
    # Two windows open together write one record between them
    await asyncio.gather(
        recorder.record_target(target, fetcher, journal),
        recorder.record_target(other, fetcher, journal),
    )

    [record] = [
        e for e in map(json.loads, log.getvalue().splitlines()) if e["event"] == "resources"
    ]
    assert record["ticker"] == "HD"
    assert record["chrome_processes"] == 0  # no Chrome in this window
    assert "bot_cpu_s" in record


@pytest.mark.skipif(sys.platform != "win32", reason="Chrome's hidden desktop is Windows only")
def test_chrome_runs_below_normal_priority_under_a_memory_ceiling() -> None:
    """So the owner's own programs come first, and a runaway page can't use up the PC's memory."""
    if sys.platform != "win32":  # the marker skips it; this tells the type checker
        pytest.skip("Windows only")
    from earnings_bot._windows_desktop import CHROME_MEMORY_LIMIT, DesktopProcess

    try:
        child = DesktopProcess(Path(sys.executable), ["-c", "import time; time.sleep(30)"])
    except OSError as error:
        pytest.skip(f"no hidden desktop here: {error}")
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, child.pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        assert kernel.GetPriorityClass(ctypes.c_void_p(handle)) == 0x4000  # below normal
        kernel.CloseHandle(ctypes.c_void_p(handle))
        used = child.usage()
        assert used is not None and used[0] == 1
        from earnings_bot._windows_desktop import _EXTENDED_LIMITS, _ExtendedLimits

        limits = _ExtendedLimits()
        assert child._kernel.QueryInformationJobObject(
            child._job, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits), None
        )
        assert limits.JobMemoryLimit == CHROME_MEMORY_LIMIT
    finally:
        child.close()
