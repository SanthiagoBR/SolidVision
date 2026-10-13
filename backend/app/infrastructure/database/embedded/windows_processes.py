"""The process table, through `kernel32` (RFC-033 sections 8.4 and 9.3).

Four questions, each one call or two into `kernel32` via `ctypes`, in the
style of the volume adapter and without a new dependency: which processes
exist and who their parents are (`CreateToolhelp32Snapshot`), where a
process's executable is (`QueryFullProcessImageNameW`), whether a PID is
still alive, and how to end one (`TerminateProcess`).

Listing processes through PowerShell, as the RFC-033a script does, costs
seconds per call -- more than a whole warm start should (section 2.1).
"""

from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import EmbeddedPostgresError

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_TERMINATE_WAIT_MS = 5000
_ERROR_ACCESS_DENIED = 5
_MAX_PATH = 32768


@dataclass(frozen=True)
class ProcessInfo:
    """One row of a process snapshot."""

    pid: int
    parent_pid: int
    name: str


class ProcessTable(ABC):
    """Port over the operating system's process table.

    The adapter decides who to adopt and who to end from these answers, so
    the unit tests of the state machine (section 12) stand a double in for
    the real table.
    """

    @abstractmethod
    def snapshot(self) -> list[ProcessInfo]:
        """Every process, with its parent PID, at this instant."""

    @abstractmethod
    def executable(self, pid: int) -> Path | None:
        """The full path of a process's image, or `None` if it cannot be read."""

    @abstractmethod
    def is_alive(self, pid: int) -> bool:
        """Whether a process with this PID is running now."""

    @abstractmethod
    def terminate(self, pid: int) -> bool:
        """End a process and wait for it to be gone.

        `False` when it was already gone or refused.
        """


class WindowsProcesses(ProcessTable):
    """`ProcessTable` over `kernel32`. Refuses to be built off Windows."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise EmbeddedPostgresError(
                f"WindowsProcesses requires Windows; this is {sys.platform!r}."
            )

    @staticmethod
    def _kernel32() -> Any:
        """`kernel32` with the signatures this module calls, set every time.

        Typed `Any` for the reason the volume adapter gives: a `ctypes` DLL
        has no static signature. The `argtypes` and `restype` are what keep
        64-bit handles from being truncated to a C `int`.
        """
        from ctypes import wintypes

        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return kernel32

    def snapshot(self) -> list[ProcessInfo]:
        kernel32 = self._kernel32()
        handle = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise EmbeddedPostgresError(
                "Could not snapshot the process table "
                f"(Windows error {ctypes.get_last_error()})."
            )
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
            found: list[ProcessInfo] = []
            more = kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while more:
                found.append(
                    ProcessInfo(
                        pid=int(entry.th32ProcessID),
                        parent_pid=int(entry.th32ParentProcessID),
                        name=str(entry.szExeFile),
                    )
                )
                more = kernel32.Process32NextW(handle, ctypes.byref(entry))
            return found
        finally:
            kernel32.CloseHandle(handle)

    def executable(self, pid: int) -> Path | None:
        from ctypes import wintypes

        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(_MAX_PATH)
            size = wintypes.DWORD(_MAX_PATH)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def is_alive(self, pid: int) -> bool:
        """Alive means "has not exited", asked of the process object itself.

        `WaitForSingleObject` rather than `GetExitCodeProcess`, which cannot
        tell a running process from one that exited with code 259. A process
        Windows will not open for this user is alive: refusing access is
        something only an existing process can do.
        """
        if pid <= 0:
            return False
        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
        )
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
        finally:
            kernel32.CloseHandle(handle)

    def terminate(self, pid: int) -> bool:
        """End a process and wait until Windows reports it gone.

        `TerminateProcess` only asks: it returns while the process object is
        still unsignaled, so an `is_alive` right after it still says yes (49
        of 50 tries). A caller that restarts the cluster at once would then
        adopt a postmaster that is dying.
        """
        kernel32 = self._kernel32()
        handle = kernel32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            if not kernel32.TerminateProcess(handle, 1):
                return False
            kernel32.WaitForSingleObject(handle, _TERMINATE_WAIT_MS)
            return True
        finally:
            kernel32.CloseHandle(handle)


class _ProcessEntry32W(ctypes.Structure):
    """`PROCESSENTRY32W`, as `tlhelp32.h` declares it."""

    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]
