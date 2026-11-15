"""Start and reap a Chrome process on a Windows desktop that is never displayed.

Chrome runs in a Windows job object that ends it, and every process it started, when the job's
last handle closes, which happens when the bot exits for any reason. Before that, a bot stopped
for a deploy or a crash left its Chrome running: one from Nov 13 was still running on Nov 15,
its parent gone, with 18 old profiles (1.8 GB) in the temp folder.

The job also keeps Chrome from crowding the PC: below-normal priority, so the owner's own programs
come first when the CPU is busy, and a ceiling on the memory all its processes may commit. Five
release windows at once peaked at 1.6 GB working set (1.0 GB committed) and 1.4 cores; a /track
research run at 1.2 GB and 2.1 cores (measured Nov 15 on a 32-thread, 64 GB PC).
"""

import ctypes
import os
import subprocess
import sys
import uuid
from ctypes import wintypes as w
from pathlib import Path


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.POINTER(w.BYTE)),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class _Process(ctypes.Structure):
    _fields_ = [
        ("hProcess", w.HANDLE),
        ("hThread", w.HANDLE),
        ("dwProcessId", w.DWORD),
        ("dwThreadId", w.DWORD),
    ]


def chrome_path() -> Path:
    if sys.platform != "win32":
        raise OSError("The desktop Chrome trial requires Windows")
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if directory := os.environ.get(variable):
            path = Path(directory) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if path.is_file():
                return path
    raise OSError("Google Chrome was not found in its standard Windows locations")


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", w.LARGE_INTEGER),
        ("PerJobUserTimeLimit", w.LARGE_INTEGER),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_BASIC_ACCOUNTING = 1  # JobObjectBasicAccountingInformation
_EXTENDED_LIMITS = 9  # JobObjectExtendedLimitInformation
_KILL_ON_JOB_CLOSE = 0x2000
_LIMIT_PRIORITY_CLASS = 0x20
_LIMIT_JOB_MEMORY = 0x200
_BELOW_NORMAL = 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
# The most memory all of Chrome's processes may commit together: several times the measured peak,
# and an eighth of the PC's 64 GB. Past it a tab fails, and its route falls back like any other
# Chrome error, instead of the PC running short.
CHROME_MEMORY_LIMIT = 8 * 1024**3


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", w.LARGE_INTEGER),
        ("TotalKernelTime", w.LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", w.LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", w.LARGE_INTEGER),
        ("TotalPageFaultCount", w.DWORD),
        ("TotalProcesses", w.DWORD),
        ("ActiveProcesses", w.DWORD),
        ("TotalTerminatedProcesses", w.DWORD),
    ]


_CREATE_SUSPENDED = 0x4


class DesktopProcess:
    _user: ctypes.CDLL
    _kernel: ctypes.CDLL
    _desktop: int
    _process: _Process
    _job: int | None

    def __init__(self, exe: Path, args: list[str]) -> None:
        if sys.platform != "win32":
            raise OSError("The desktop Chrome trial requires Windows")
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user.CreateDesktopW.argtypes = [
            w.LPCWSTR,
            w.LPCWSTR,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
        ]
        self._user.CreateDesktopW.restype = w.HANDLE
        self._user.CloseDesktop.argtypes = [w.HANDLE]
        self._kernel.CloseHandle.argtypes = [w.HANDLE]
        self._kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self._kernel.WaitForSingleObject.restype = w.DWORD
        self._kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self._kernel.CreateProcessW.argtypes = [
            w.LPCWSTR,
            w.LPWSTR,
            w.LPVOID,
            w.LPVOID,
            w.BOOL,
            w.DWORD,
            w.LPVOID,
            w.LPCWSTR,
            ctypes.POINTER(_Startup),
            ctypes.POINTER(_Process),
        ]
        self._kernel.CreateProcessW.restype = w.BOOL
        self._kernel.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = w.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        self._kernel.SetInformationJobObject.restype = w.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = w.BOOL
        self._kernel.ResumeThread.argtypes = [w.HANDLE]
        self._kernel.ResumeThread.restype = w.DWORD
        self._kernel.QueryInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            w.LPVOID,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        self._kernel.QueryInformationJobObject.restype = w.BOOL

        name = f"EarningsBot-{uuid.uuid4().hex}"
        generic_all = 0x10000000
        self._desktop = self._user.CreateDesktopW(name, None, None, 0, generic_all, None)
        if not self._desktop:
            raise ctypes.WinError(ctypes.get_last_error())

        startup = _Startup()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = name
        self._process = _Process()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(exe), *args]))
        self._job = self._kill_on_close_job()
        if not self._kernel.CreateProcessW(
            str(exe),
            command,
            None,
            None,
            False,
            _CREATE_SUSPENDED,  # joins the job before it can start anything
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(self._process),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self._close_job()
            self._user.CloseDesktop(self._desktop)
            raise error
        if self._job and not self._kernel.AssignProcessToJobObject(
            self._job, self._process.hProcess
        ):
            self._close_job()  # Chrome still runs; it just isn't ended with the bot
        self._kernel.ResumeThread(self._process.hThread)
        self._kernel.CloseHandle(self._process.hThread)

    def _kill_on_close_job(self) -> int | None:
        """A job that ends its processes when its last handle closes, or None if Windows won't
        make one."""
        job = self._kernel.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            _KILL_ON_JOB_CLOSE | _LIMIT_PRIORITY_CLASS | _LIMIT_JOB_MEMORY
        )
        limits.BasicLimitInformation.PriorityClass = _BELOW_NORMAL
        limits.JobMemoryLimit = CHROME_MEMORY_LIMIT
        if not self._kernel.SetInformationJobObject(
            job, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self._kernel.CloseHandle(job)
            return None
        return job

    def _close_job(self) -> None:
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None

    @property
    def pid(self) -> int:
        return self._process.dwProcessId

    def usage(self) -> tuple[int, float, int] | None:
        """Chrome's processes now, the CPU seconds they've used in all, and the most memory they
        have committed together (bytes), from its job. None without a job."""
        if not self._job:
            return None
        accounting, limits = _Accounting(), _ExtendedLimits()
        if not self._kernel.QueryInformationJobObject(
            self._job, _BASIC_ACCOUNTING, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ) or not self._kernel.QueryInformationJobObject(
            self._job, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits), None
        ):
            return None
        cpu = (accounting.TotalUserTime + accounting.TotalKernelTime) / 10_000_000
        return accounting.ActiveProcesses, cpu, limits.PeakJobMemoryUsed

    @property
    def ended_with_owner(self) -> bool:
        """Whether Chrome and its processes end when this process does."""
        return self._job is not None

    @property
    def running(self) -> bool:
        return self._kernel.WaitForSingleObject(self._process.hProcess, 0) == 258

    def close(self) -> None:
        handle = self._process.hProcess
        if self._kernel.WaitForSingleObject(handle, 2000) == 258:  # WAIT_TIMEOUT
            self._kernel.TerminateProcess(handle, 1)
            self._kernel.WaitForSingleObject(handle, 2000)
        self._kernel.CloseHandle(handle)
        self._close_job()  # ends any of Chrome's processes still running
        self._user.CloseDesktop(self._desktop)
