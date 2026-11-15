"""How much of the PC the bot is using, for the journal during release windows: its own memory and
CPU, its Chrome's (from Chrome's job object), and the memory the PC has left. Five windows at
once used 1.6 GB and 1.4 cores of Chrome at their peak, and the bot itself under 130 MB (measured
Nov 15); these records show the same on every live release."""

import ctypes
import sys
import time
from collections.abc import Iterable
from ctypes import wintypes as w
from typing import Any, Protocol

MB = 1024 * 1024


class _Usage(Protocol):
    def usage(self) -> tuple[int, float, int] | None: ...


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("PageFaultCount", w.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", w.DWORD),
        ("dwMemoryLoad", w.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _own_memory() -> tuple[int, int] | None:
    """This process's working set and private bytes."""
    if sys.platform != "win32":
        return None
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        return None
    return counters.WorkingSetSize, counters.PrivateUsage


def _free_memory() -> int | None:
    if sys.platform != "win32":
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.ullAvailPhys


def snapshot(chromes: Iterable[_Usage]) -> dict[str, Any]:
    """The bot's own use, its Chromes' together, and the PC's free memory, in MB and seconds."""
    record: dict[str, Any] = {"bot_cpu_s": round(time.process_time(), 1)}
    if own := _own_memory():
        record["bot_mb"], record["bot_private_mb"] = own[0] // MB, own[1] // MB
    processes, cpu, peak = 0, 0.0, 0
    for chrome in chromes:
        if used := chrome.usage():
            processes += used[0]
            cpu += used[1]
            peak += used[2]
    record["chrome_processes"] = processes
    record["chrome_cpu_s"] = round(cpu, 1)
    record["chrome_peak_mb"] = peak // MB
    if (free := _free_memory()) is not None:
        record["free_mb"] = free // MB
    return record
