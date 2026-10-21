r"""A signal to the postmaster, sent the way `pg_ctl kill` sends it (RFC-036 7.5).

PostgreSQL on Windows has no signals of the system's: each server process
listens on a named pipe of its own, `\\.\pipe\pgsignal_<pid>`, for one byte --
the number of the signal -- and answers with the same byte. `pg_ctl kill` is
one `CallNamedPipe` on that pipe (`src/port/kill.c`), and so is `send()`.

What it is for: a stop that creates no process. During a logoff, a process
that starts dies in its DLL initialization (`0xC000026B`,
`STATUS_DLL_INIT_FAILED_LOGOFF`), and `pg_ctl` is one -- measured on
2026-10-20, *"Neither a fast nor an immediate shutdown stopped the cluster"*,
with nothing in `pg_ctl.log`. The launcher, already running, can still write
to a pipe, and a fast or an immediate shutdown uses only the server processes
that already exist.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

FAST = 2
"""`SIGINT`, the postmaster's fast shutdown: what `pg_ctl stop -m fast` sends."""

IMMEDIATE = 3
"""`SIGQUIT`, the immediate shutdown: the next start recovers from the WAL."""

TIMEOUT_MS = 1000
"""How long `CallNamedPipe` waits for the pipe, as `pg_ctl kill` does."""


def send(pid: int, signal: int) -> bool:
    """Send `signal` to the server process `pid`; whether it said it got it."""
    if sys.platform != "win32":
        return False
    sent = ctypes.c_ubyte(signal)
    answer = ctypes.c_ubyte(0)
    read = wintypes.DWORD(0)
    if not _kernel32().CallNamedPipeW(
        rf"\\.\pipe\pgsignal_{pid}",
        ctypes.byref(sent),
        1,
        ctypes.byref(answer),
        1,
        ctypes.byref(read),
        TIMEOUT_MS,
    ):
        return False
    return read.value == 1 and answer.value == signal


def _kernel32() -> Any:
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CallNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    kernel32.CallNamedPipeW.restype = wintypes.BOOL
    return kernel32
