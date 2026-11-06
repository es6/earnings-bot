"""Start and reap a Chrome process on a Windows desktop that is never displayed."""

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


class DesktopProcess:
    _user: ctypes.CDLL
    _kernel: ctypes.CDLL
    _desktop: int
    _process: _Process

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
        if not self._kernel.CreateProcessW(
            str(exe),
            command,
            None,
            None,
            False,
            0,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(self._process),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self._user.CloseDesktop(self._desktop)
            raise error
        self._kernel.CloseHandle(self._process.hThread)

    @property
    def running(self) -> bool:
        return self._kernel.WaitForSingleObject(self._process.hProcess, 0) == 258

    def close(self) -> None:
        handle = self._process.hProcess
        if self._kernel.WaitForSingleObject(handle, 2000) == 258:  # WAIT_TIMEOUT
            self._kernel.TerminateProcess(handle, 1)
            self._kernel.WaitForSingleObject(handle, 2000)
        self._kernel.CloseHandle(handle)
        self._user.CloseDesktop(self._desktop)
