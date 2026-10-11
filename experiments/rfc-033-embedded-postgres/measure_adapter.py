"""The embedded PostgreSQL adapter, measured (RFC-033 section 18).

RFC-033a measured that the database starts once. This measures what only
happens from the second start on, against the adapter of
`app/infrastructure/database/embedded/`: the first run with each part timed,
the warm start decomposed, adoption, stop, a postmaster killed under write
load, orphans and decoys, an interrupted `initdb`, a start killed during
recovery, a busy port, a reserved range, two starts at once, a caller without
a console, Ctrl+C, paths outside the ANSI code page, the DLL closure of the
runtime, the user's `PG*` environment, the DACL, and whether the password
leaks anywhere.

Every cluster lives under a temporary work directory. Nothing here goes near
`%LOCALAPPDATA%\\SolidVision`, and nothing is left running: the last check
looks for processes of the runtime, and the work directory is removed.

Run it with the project's virtualenv, from the repository root, with the
output redirected next to this script (`*.log` is outside git, like every
experiment's log):

    python experiments/rfc-033-embedded-postgres/measure_adapter.py > LOG 2>&1

    --list                 the checks, and which are opt-in
    --only a,b             just these
    --skip a,b             all the default ones but these
    --with suite,...       add opt-in checks (they take minutes)
    --kills N              kills per kill-under-load check (default 3)
    --black-python PATH    an interpreter that can run black (it refuses 3.12.5)
    --keep                 keep the work directory

The opt-in checks are the three that run a test suite: `suite`,
`embedded-tests` and `docker-suite`. Two checks are manual by nature and only
print what to do: `logoff`, and `elevated` when this process is not elevated,
because elevating needs a UAC prompt.

`no-console` opens a console window on the desktop for a moment, on purpose:
it runs a *control* without `CREATE_NO_WINDOW`, to show that the method
detects the problem the flag exists to prevent. `ctrl-c` has a control too,
in a console of its own that is asked to stay hidden.

No new dependency: `ctypes` over `kernel32`, `user32`, `version`, `advapi32`,
`sfc` and `ntdll`, a PE import-table reader written with `struct`, and the
`psycopg` the project already has. `read_facts` is imported from the RFC-033a
script beside this one, so the facts are read by the same code.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import winreg
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psycopg

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))

from app.infrastructure.database.embedded import database_setup  # noqa: E402
from app.infrastructure.database.embedded.cluster import (  # noqa: E402
    ClusterStatus,
    EmbeddedPostgres,
    Phase,
    PortProber,
    PostgresCommands,
)
from app.infrastructure.database.embedded.cluster_home import (  # noqa: E402
    CREATE_NO_WINDOW,
    ClusterFiles,
    ClusterHome,
    current_user_sid,
)
from app.infrastructure.database.embedded.database_setup import (  # noqa: E402
    ClusterConnection,
)
from app.infrastructure.database.embedded.errors import (  # noqa: E402
    ClusterRefusedError,
    EmbeddedPostgresError,
)
from app.infrastructure.database.embedded.postmaster_pid import (  # noqa: E402
    read_postmaster_pid,
)
from app.infrastructure.database.embedded.runtime import (  # noqa: E402
    PostgresRuntime,
)
from app.infrastructure.database.embedded.windows_processes import (  # noqa: E402
    WindowsProcesses,
)

RUNTIME_DIRECTORY = REPO / "runtime" / "postgres"
MAJOR = 16

# RFC-033a section 13, the limits of the non-regression rule (section 18),
# measured on its machine: an HDD. And the same script run on *this* machine,
# when this one is not that one, in `measure_embedded_postgres.<date>.log`.
RFC033A = {
    "initdb": (24.7, 38.9),
    "listening -> ready": (0.216, 0.235),
    "alembic upgrade head": (2.4, 2.5),
}

kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
user32: Any = ctypes.WinDLL("user32", use_last_error=True)
advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
version_dll: Any = ctypes.WinDLL("version", use_last_error=True)
ntdll: Any = ctypes.WinDLL("ntdll")

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.GetShortPathNameW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    wintypes.DWORD,
]
ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

PROCESSES = WindowsProcesses()
INVALID_HANDLE = ctypes.c_void_p(-1).value


# --------------------------------------------------------------------------
# Output: everything printed is also kept, so the last check can search it.
# --------------------------------------------------------------------------


class Tee:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.text: list[str] = []

    def write(self, data: str) -> int:
        self.text.append(data)
        return int(self._stream.write(data))

    def flush(self) -> None:
        self._stream.flush()


class AdapterLog(logging.Handler):
    """The adapter's own log records, to quote what it said it did."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


ADAPTER_LOG = AdapterLog()


def say(text: str = "") -> None:
    print(f"    {text}" if text else "")
    sys.stdout.flush()


def seconds(value: float) -> str:
    return f"{value:.3f} s" if value < 10 else f"{value:.1f} s"


def spread(values: list[float]) -> str:
    if not values:
        return "no samples"
    if len(values) == 1:
        return seconds(values[0])
    return (
        f"median {seconds(statistics.median(values))} "
        f"(min {seconds(min(values))}, max {seconds(max(values))}, n={len(values)})"
    )


def mb(size: float) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


class CheckFailedError(Exception):
    """A check's criterion was not met. The message is the result."""


# --------------------------------------------------------------------------
# Windows, asked directly.
# --------------------------------------------------------------------------


def taskkill(pid: int, *, tree: bool = False) -> None:
    """`taskkill /F`, the way Task Manager ends a process (section 9.4)."""
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    arguments = [str(system32 / "taskkill.exe"), "/F", "/PID", str(pid)]
    if tree:
        arguments.insert(2, "/T")
    subprocess.run(arguments, capture_output=True, stdin=subprocess.DEVNULL)


def children_of(pid: int) -> list[int]:
    return [p.pid for p in PROCESSES.snapshot() if p.parent_pid == pid]


def command_lines(pids: list[int]) -> dict[int, str]:
    """What each process was started as: `postgres.exe --forkbackend ...`.

    Through PowerShell, which takes about a second: for a report, never for
    a decision.
    """
    if not pids:
        return {}
    wanted = " or ".join(f"ProcessId={pid}" for pid in pids)
    done = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f'Get-CimInstance Win32_Process -Filter "{wanted}" | '
            'ForEach-Object { "$($_.ProcessId)|$($_.CommandLine)" }',
        ],
        capture_output=True,
        text=True,
    )
    found = {}
    for line in done.stdout.splitlines():
        pid, _, command = line.partition("|")
        if pid.strip().isdigit():
            found[int(pid)] = command.strip()
    return found


def runtime_processes(runtime: PostgresRuntime) -> list[tuple[int, str]]:
    found = []
    for process in PROCESSES.snapshot():
        executable = PROCESSES.executable(process.pid)
        if executable is not None and runtime.contains(executable):
            found.append((process.pid, executable.name))
    return found


def suspend(pid: int) -> None:
    handle = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not handle:
        raise CheckFailedError(f"could not open PID {pid} to suspend it")
    try:
        ntdll.NtSuspendProcess(handle)
    finally:
        kernel32.CloseHandle(handle)


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def memory_of(pid: int) -> tuple[int, int]:
    """(working set, private commit) of a process, in bytes."""
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not handle:
        handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return 0, 0
    try:
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(_MemoryCounters)
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_MemoryCounters),
            wintypes.DWORD,
        ]
        if not kernel32.K32GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return 0, 0
        return int(counters.WorkingSetSize), int(counters.PagefileUsage)
    finally:
        kernel32.CloseHandle(handle)


class _ModuleEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


def loaded_modules(pid: int) -> list[Path]:
    """Every DLL a live process has actually loaded, by full path."""
    kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    for _ in range(10):
        handle = kernel32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
        if handle and handle != INVALID_HANDLE:
            break
        time.sleep(0.05)  # ERROR_BAD_LENGTH while the module list changes
    else:
        return []
    try:
        entry = _ModuleEntry()
        entry.dwSize = ctypes.sizeof(_ModuleEntry)
        found = []
        more = kernel32.Module32FirstW(handle, ctypes.byref(entry))
        while more:
            found.append(Path(entry.szExePath))
            more = kernel32.Module32NextW(handle, ctypes.byref(entry))
        return found
    finally:
        kernel32.CloseHandle(handle)


def is_elevated(pid: int | None = None) -> bool | None:
    """Whether a process's token is elevated. `None` when it cannot be read."""
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = (
        kernel32.GetCurrentProcess()
        if pid is None
        else kernel32.OpenProcess(0x1000, False, pid)
    )
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return None
        elevation = wintypes.DWORD()
        size = wintypes.DWORD()
        token_elevation = 20
        if not advapi32.GetTokenInformation(
            token, token_elevation, ctypes.byref(elevation), 4, ctypes.byref(size)
        ):
            return None
        return bool(elevation.value)
    finally:
        if token:
            kernel32.CloseHandle(token)
        if pid is not None:
            kernel32.CloseHandle(process)


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenGroups(ctypes.Structure):
    _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", _SidAndAttributes * 1)]


ADMINISTRATORS_SID = "S-1-5-32-544"
ELEVATION_TYPES = {1: "default", 2: "full", 3: "limited"}


def _token_information(token: Any, information_class: int) -> Any:
    """One variable-length answer of `GetTokenInformation`, or `None`."""
    size = wintypes.DWORD()
    advapi32.GetTokenInformation(token, information_class, None, 0, ctypes.byref(size))
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi32.GetTokenInformation(
        token, information_class, buffer, size.value, ctypes.byref(size)
    ):
        return None
    return buffer


def token_facts(pid: int | None = None) -> str:
    """What a process's token says about administrator rights.

    Measured on 2026-10-11: a postmaster that `pg_ctl` started from an
    elevated process reads `TokenElevation = True`. That bit alone does not
    say whether `pg_ctl` restricted the token. This also reads the state of
    the Administrators group in the token (enabled, or kept only to deny)
    and how many privileges the token holds, to compare the caller's token
    with the server's.
    """
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = (
        kernel32.GetCurrentProcess()
        if pid is None
        else kernel32.OpenProcess(0x1000, False, pid)
    )
    if not process:
        return "process not opened"
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return "token not opened"
        elevation = _token_information(token, 20)
        elevation_type = _token_information(token, 18)
        groups = _token_information(token, 2)
        privileges = _token_information(token, 3)
        administrators = "not read"
        if groups is not None:
            administrators = "not in the token"
            header = _TokenGroups.from_buffer(groups)
            entries = (_SidAndAttributes * header.GroupCount).from_address(
                ctypes.addressof(groups) + _TokenGroups.Groups.offset
            )
            for entry in entries:
                text = wintypes.LPWSTR()
                if not advapi32.ConvertSidToStringSidW(entry.Sid, ctypes.byref(text)):
                    continue
                sid = text.value
                kernel32.LocalFree(text)
                if sid != ADMINISTRATORS_SID:
                    continue
                if entry.Attributes & 0x10:
                    administrators = "deny only"
                elif entry.Attributes & 0x4:
                    administrators = "enabled"
                else:
                    administrators = f"attributes {entry.Attributes:#x}"
        read = ctypes.c_uint32.from_buffer_copy
        elevated = bool(read(elevation).value) if elevation else "?"
        kind = (
            ELEVATION_TYPES.get(read(elevation_type).value) if elevation_type else "?"
        )
        held = read(privileges).value if privileges else "?"
        return (
            f"TokenElevation={elevated}, elevation type={kind}, "
            f"Administrators group: {administrators}, privileges: {held}"
        )
    finally:
        if token:
            kernel32.CloseHandle(token)
        if pid is not None:
            kernel32.CloseHandle(process)


def postgres_run_directly(runtime: PostgresRuntime, missing: Path) -> str:
    """`postgres.exe` started by this process itself, without `pg_ctl`.

    Pointed at a directory that does not exist, so that no server can come
    of it: what it prints is either its refusal to run with administrative
    permissions, or its complaint about the directory.
    """
    done = subprocess.run(
        [str(runtime.postgres), "-D", str(missing)],
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        env=runtime.child_environment(),
        creationflags=CREATE_NO_WINDOW,
        timeout=60,
    )
    said = (done.stderr or done.stdout).strip().splitlines()
    return f"exit {done.returncode}: {said[0] if said else '(nothing)'}"


_ENUM_WINDOWS = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def visible_windows() -> dict[int, tuple[str, str, int]]:
    """Top-level visible windows: handle -> (class, title, owning PID)."""
    found: dict[int, tuple[str, str, int]] = {}

    def visit(window: int, _: int) -> bool:
        if user32.IsWindowVisible(window):
            name = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(512)
            pid = wintypes.DWORD()
            user32.GetClassNameW(window, name, 256)
            user32.GetWindowTextW(window, title, 512)
            user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
            found[int(window)] = (name.value, title.value, int(pid.value))
        return True

    user32.EnumWindows(_ENUM_WINDOWS(visit), 0)
    return found


CONSOLE_CLASSES = (
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
    "PseudoConsoleWindow",
)
CONSOLE_HOSTS = ("conhost.exe", "openconsole.exe", "windowsterminal.exe")


def describe_windows(windows: dict[int, tuple[str, str, int]]) -> list[str]:
    described = []
    for name, title, pid in windows.values():
        executable = PROCESSES.executable(pid)
        described.append(
            f"class {name!r}, title {title[:50]!r}, "
            f"owner {executable.name if executable else pid}"
        )
    return described


def console_windows(
    windows: dict[int, tuple[str, str, int]],
) -> dict[int, tuple[str, str, int]]:
    found = {}
    for handle, (name, title, pid) in windows.items():
        executable = PROCESSES.executable(pid)
        owner = executable.name.lower() if executable else ""
        if name in CONSOLE_CLASSES or owner in CONSOLE_HOSTS:
            found[handle] = (name, title, pid)
    return found


def open_to_the_user(path: Path) -> str:
    """Grant the user's own SID a directory that an elevated process made.

    `tempfile.mkdtemp` writes a protected DACL with three entries: the
    system, the administrators and the owner. None names the user. Measured
    on 2026-10-11, five times out of five, with this script run elevated: a
    process of the same user that is not elevated can neither list such a
    directory nor read its ACL, and `initdb`, started by the elevated
    process, fails under it on the first directory it meets ("could not
    create directory ...: File exists"). That is this script's directory,
    not the adapter's, so the check never reached what it asks.

    Returns the descriptor the directory had, owner included, and the grant.
    """
    before = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Acl -LiteralPath '{path}').Sddl",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    granted = subprocess.run(
        ["icacls", str(path), "/grant", f"*{current_user_sid()}:(OI)(CI)F"],
        capture_output=True,
        text=True,
    )
    return (
        f"was {before or 'not read'}; then granted to the user's SID "
        f"(icacls exit {granted.returncode})"
    )


def dacl_sddl(path: Path) -> str:
    """The DACL as SDDL: accounts by SID, never by a translated name."""
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    descriptor = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x4, None, None, None, None, ctypes.byref(descriptor)
    )
    if error:
        raise CheckFailedError(f"GetNamedSecurityInfoW({path}) failed: {error}")
    try:
        text = wintypes.LPWSTR()
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 0x4, ctypes.byref(text), None
        )
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


def short_path(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetShortPathNameW(str(path), buffer, 32768):
        return ""
    return buffer.value


def windows_protects(path: Path) -> bool:
    """Whether Windows Resource Protection covers the file: it is Windows' own.

    Asked of Windows rather than read from the product name, which is
    translated -- "Sistema Operacional Microsoft® Windows®" on the machine
    this was written on. A `vcruntime140.dll` that a redistributable put in
    `System32` is not protected; `ucrtbase.dll`, part of Windows since 10, is.
    """
    sfc: Any = ctypes.WinDLL("sfc")
    sfc.SfcIsFileProtected.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    return bool(sfc.SfcIsFileProtected(None, str(path)))


def product_name(path: Path) -> str:
    """`ProductName` from a file's version resource: who ships this DLL."""
    version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    version_dll.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    size = version_dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(str(path), 0, size, data):
        return ""
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version_dll.VerQueryValueW(
        data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return ""
    if not pointer.value or length.value < 4:
        return ""
    language, codepage = struct.unpack("<HH", ctypes.string_at(pointer.value, 4))
    key = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\ProductName"
    if not version_dll.VerQueryValueW(
        data, key, ctypes.byref(pointer), ctypes.byref(length)
    ):
        return ""
    return ctypes.wstring_at(pointer.value, max(length.value - 1, 0))


# --------------------------------------------------------------------------
# A PE import-table reader, with the standard library (section 5.3).
# --------------------------------------------------------------------------


def pe_imports(path: Path) -> list[str]:
    """The DLL names a PE file imports, load-time and delay-loaded."""
    data = path.read_bytes()
    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise CheckFailedError(f"{path} is not a PE file")
    section_count, optional_size = struct.unpack_from("<H12xH", data, pe_offset + 6)
    optional = pe_offset + 24
    (magic,) = struct.unpack_from("<H", data, optional)
    directories = optional + (112 if magic == 0x20B else 96)
    sections = []
    table = optional + optional_size
    for index in range(section_count):
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, table + index * 40 + 8
        )
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))

    def offset(rva: int) -> int:
        for virtual_address, size, raw_offset in sections:
            if virtual_address <= rva < virtual_address + size:
                return rva - virtual_address + raw_offset
        raise CheckFailedError(f"{path}: RVA {rva:#x} is in no section")

    def name_at(rva: int) -> str:
        start = offset(rva)
        return data[start : data.index(b"\0", start)].decode("ascii")

    names = []
    import_rva, _ = struct.unpack_from("<II", data, directories + 1 * 8)
    if import_rva:
        position = offset(import_rva)
        while True:
            descriptor = struct.unpack_from("<IIIII", data, position)
            if not any(descriptor):
                break
            names.append(name_at(descriptor[3]))
            position += 20
    delay_rva, _ = struct.unpack_from("<II", data, directories + 13 * 8)
    if delay_rva:
        position = offset(delay_rva)
        while True:
            descriptor = struct.unpack_from("<IIIIIIII", data, position)
            if not any(descriptor):
                break
            names.append(name_at(descriptor[1]))
            position += 32
    return names


def known_dlls() -> set[str]:
    """`KnownDLLs`: names the loader takes from System32 before looking."""
    key = r"SYSTEM\CurrentControlSet\Control\Session Manager\KnownDLLs"
    names = set()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
        index = 0
        while True:
            try:
                _, value, _ = winreg.EnumValue(handle, index)
            except OSError:
                break
            if isinstance(value, str) and value.lower().endswith(".dll"):
                names.add(value.lower())
            index += 1
    return names


# --------------------------------------------------------------------------
# Timing the adapter's own parts, by standing in for its collaborators.
# --------------------------------------------------------------------------


class Timings:
    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}

    def add(self, name: str, value: float) -> None:
        self.samples.setdefault(name, []).append(value)

    @contextlib.contextmanager
    def measure(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - started)

    def last(self, name: str) -> float:
        return self.samples[name][-1]

    def reset(self) -> None:
        self.samples.clear()


TIMINGS = Timings()


class TimedRuntime(PostgresRuntime):
    def manifest(self) -> Any:
        with TIMINGS.measure("runtime check"):
            return super().manifest()


class TimedCommands(PostgresCommands):
    def initdb(
        self, target: Path, password_file: Path, timeout: float
    ) -> tuple[int, str]:
        with TIMINGS.measure("initdb.exe"):
            return super().initdb(target, password_file, timeout)

    def start(self, files: ClusterFiles, timeout: int) -> int:
        with TIMINGS.measure("pg_ctl start -w"):
            return super().start(files, timeout)

    def stop(self, files: ClusterFiles, mode: str, timeout: int) -> int:
        pid_file = read_postmaster_pid(files.postmaster_pid)
        server = [pid_file.pid, *children_of(pid_file.pid)] if pid_file else []
        with TIMINGS.measure(f"pg_ctl stop -m {mode}"):
            returncode = super().stop(files, mode, timeout)
        # `pg_ctl -w` returns when the lock file is gone; the processes, a
        # moment later. How much later is what this measures.
        returned = time.perf_counter()
        alive_at_return = sum(PROCESSES.is_alive(pid) for pid in server)
        while any(PROCESSES.is_alive(pid) for pid in server):
            if time.perf_counter() - returned > 30:
                break
            time.sleep(0.001)
        TIMINGS.add(
            "processes gone after pg_ctl stop returned", time.perf_counter() - returned
        )
        TIMINGS.add("processes alive when pg_ctl stop returned", float(alive_at_return))
        return returncode


def timed_login(connection: ClusterConnection) -> None:
    with TIMINGS.measure("connection"):
        database_setup.verify_login(connection)


def time_prepare_steps() -> None:
    """Wrap the steps of `prepare_database` where it looks them up."""
    for name in (
        "ensure_database",
        "code_revisions",
        "database_heads",
        "upgrade",
        "check_facts",
    ):
        original = getattr(database_setup, name)

        def timed(*args: Any, _original: Any = original, _name: str = name) -> Any:
            with TIMINGS.measure(_name):
                return _original(*args)

        setattr(database_setup, name, timed)


SERVER_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})")


def server_log(files: ClusterFiles) -> str:
    text = []
    if files.server_log_directory.is_dir():
        for path in sorted(files.server_log_directory.iterdir()):
            text.append(path.read_text(encoding="utf-8", errors="replace"))
    return "".join(text)


def last_interval(log: str, first: str, second: str) -> float | None:
    """Seconds from the last line holding `first` to the next holding `second`."""
    begin = end = None
    for line in log.splitlines():
        stamp = SERVER_STAMP.match(line)
        if not stamp:
            continue
        moment = datetime.datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S.%f")
        if first in line:
            begin, end = moment, None
        elif second in line and begin is not None and end is None:
            end = moment
    if begin is None or end is None:
        return None
    return (end - begin).total_seconds()


# --------------------------------------------------------------------------
# A client that writes, and knows what the server confirmed (section 9.4).
# --------------------------------------------------------------------------


class Writer(threading.Thread):
    """Commits one row at a time and records the last commit it was told of."""

    def __init__(self, connection: ClusterConnection) -> None:
        super().__init__(daemon=True)
        self._connection = connection
        self.confirmed = -1
        self.backend_pid = 0
        self.error = ""
        self.started_writing = threading.Event()
        self.stop_requested = False

    def run(self) -> None:
        try:
            with psycopg.connect(
                self._connection.conninfo("postgres"), autocommit=True
            ) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS rfc033_confirmed "
                    "(n integer PRIMARY KEY, filler text)"
                )
                row = db.execute(
                    "SELECT coalesce(max(n), -1), pg_backend_pid() "
                    "FROM rfc033_confirmed"
                ).fetchone()
                assert row is not None
                number, self.backend_pid = int(row[0]), int(row[1])
                self.confirmed = number
                while not self.stop_requested:
                    number += 1
                    db.execute(
                        "INSERT INTO rfc033_confirmed VALUES (%s, %s)",
                        [number, "x" * 200],
                    )
                    self.confirmed = number
                    self.started_writing.set()
        except psycopg.Error as error:
            self.error = str(error).strip().splitlines()[0][:100]
        finally:
            self.started_writing.set()


def confirmed_rows(connection: ClusterConnection, confirmed: int) -> tuple[int, int]:
    """(rows present up to `confirmed`, rows that should be there)."""
    with psycopg.connect(connection.conninfo("postgres"), autocommit=True) as db:
        row = db.execute(
            "SELECT count(*) FROM rfc033_confirmed WHERE n <= %s", [confirmed]
        ).fetchone()
    assert row is not None
    return int(row[0]), confirmed + 1


# --------------------------------------------------------------------------
# The measurement.
# --------------------------------------------------------------------------

ADAPTER_CHILD = """
import json, sys, time
from pathlib import Path
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.errors import EmbeddedPostgresError
from app.infrastructure.database.embedded.postmaster_pid import read_postmaster_pid
from app.infrastructure.database.embedded.runtime import PostgresRuntime

runtime, home, actions = sys.argv[1], Path(sys.argv[2]), sys.argv[3].split(",")
cluster = EmbeddedPostgres(PostgresRuntime(Path(runtime)), ClusterHome(home))
result = {}
try:
    began = time.perf_counter()
    started = cluster.start()
    result.update(
        started_here=started.started_here,
        created=started.created,
        recovered=started.recovered,
        port=started.connection.port,
        start_seconds=time.perf_counter() - began,
    )
    pid_file = read_postmaster_pid(home / "16" / "data" / "postmaster.pid")
    result["postmaster"] = pid_file.pid if pid_file else None
    if "prepare" in actions:
        prepared = cluster.prepare(started.connection)
        result["heads"] = list(prepared.heads)
    if "stop" in actions:
        result["stop"] = cluster.stop().value
except EmbeddedPostgresError as error:
    result["error"] = f"{type(error).__name__}: {error}"
print("RESULT " + json.dumps(result), flush=True)
"""


class Measurement:
    def __init__(self, work: Path, black_python: str | None, kills: int = 3) -> None:
        self.work = work
        self.black_python = black_python
        self.kills = kills
        self.runtime = PostgresRuntime(RUNTIME_DIRECTORY)
        self.verdict: dict[str, str] = {}
        self._template: Path | None = None
        self._runtime_copy: PostgresRuntime | None = None
        self.tee = Tee(sys.stdout)
        self.cli_output: list[str] = []
        self.window_notes: list[str] = []
        self.preexisting = {pid for pid, _ in runtime_processes(self.runtime)}

    # -- clusters ------------------------------------------------------------

    def home(self, name: str) -> Path:
        return self.work / name / "SolidVision" / "postgres"

    def cluster(
        self,
        home: Path,
        *,
        runtime: PostgresRuntime | None = None,
        timed: bool = False,
        **collaborators: Any,
    ) -> EmbeddedPostgres:
        chosen = runtime or self.runtime
        if timed:
            chosen = TimedRuntime(chosen.root)
            collaborators.setdefault("commands", TimedCommands(chosen))
            collaborators.setdefault("login_check", timed_login)
        return EmbeddedPostgres(chosen, ClusterHome(home), **collaborators)

    def template(self) -> Path:
        """A prepared, stopped cluster to copy: one `initdb` for many checks."""
        if self._template is None:
            home = self.home("template")
            cluster = self.cluster(home)
            started = cluster.start()
            cluster.prepare(started.connection)
            cluster.stop()
            self._template = home
        return self._template

    def copy_of_template(self, name: str) -> Path:
        home = self.home(name)
        shutil.copytree(self.template(), home)
        return home

    def runtime_copy(self) -> PostgresRuntime:
        """The runtime in another directory, one with a space in its name."""
        if self._runtime_copy is None:
            target = self.work / "Solid Vision Runtime" / "postgres"
            shutil.copytree(self.runtime.root, target)
            self._runtime_copy = PostgresRuntime(target)
        return self._runtime_copy

    def child(
        self,
        home: Path,
        actions: str = "start",
        *,
        runtime: PostgresRuntime | None = None,
        executable: str | None = None,
        environment: dict[str, str] | None = None,
        **popen: Any,
    ) -> subprocess.Popen[str]:
        """`start()` in another process: something to kill, or to race."""
        env = dict(environment if environment is not None else os.environ)
        env["PYTHONPATH"] = str(BACKEND)
        return subprocess.Popen(
            [
                executable or sys.executable,
                "-c",
                ADAPTER_CHILD,
                str((runtime or self.runtime).root),
                str(home),
                actions,
            ],
            cwd=BACKEND,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            **popen,
        )

    @staticmethod
    def child_result(process: subprocess.Popen[str], timeout: float = 300) -> Any:
        output, _ = process.communicate(timeout=timeout)
        for line in output.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line.removeprefix("RESULT "))
        return {"error": f"no result (exit {process.returncode})"}

    def stop_quietly(self, cluster: EmbeddedPostgres) -> None:
        with contextlib.suppress(EmbeddedPostgresError):
            cluster.stop()

    def ours_running(self) -> list[tuple[int, str]]:
        """The processes this run is answerable for, and no others.

        A server whose lock file is under the work directory, its children,
        and anything running from a copy of the runtime made there. Never a
        cluster of the same runtime that was already up, or that someone else
        starts meanwhile: a real one in `%LOCALAPPDATA%` is not this script's
        to count, let alone to end.
        """
        found: dict[int, str] = {}
        snapshot = PROCESSES.snapshot()
        for lock_file in self.work.rglob("postmaster.pid"):
            pid_file = read_postmaster_pid(lock_file)
            if pid_file is None or not PROCESSES.is_alive(pid_file.pid):
                continue
            executable = PROCESSES.executable(pid_file.pid)
            if executable is None or executable.name.lower() != "postgres.exe":
                continue
            found[pid_file.pid] = executable.name
            for process in snapshot:
                if process.parent_pid == pid_file.pid:
                    found[process.pid] = process.name
        for process in snapshot:
            executable = PROCESSES.executable(process.pid)
            if executable is not None and str(executable).lower().startswith(
                str(self.work).lower()
            ):
                found[process.pid] = executable.name
        return sorted(found.items())

    def postmaster(self, home: Path) -> int:
        pid_file = read_postmaster_pid(ClusterHome(home).files(MAJOR).postmaster_pid)
        if pid_file is None:
            raise CheckFailedError("no postmaster.pid")
        return pid_file.pid

    # -- 1. the first run ----------------------------------------------------

    def first_run(self) -> str:
        home = self.home("template")
        cluster = self.cluster(home, timed=True)
        TIMINGS.reset()
        marks: dict[Phase, float] = {}
        began = time.perf_counter()
        started = cluster.start(
            on_phase=lambda p: marks.setdefault(p, time.perf_counter())
        )
        after_start = time.perf_counter()
        prepared = cluster.prepare(
            started.connection,
            on_phase=lambda p: marks.setdefault(p, time.perf_counter()),
        )
        after_prepare = time.perf_counter()
        files = ClusterHome(home).files(MAJOR)
        ready = last_interval(
            server_log(files),
            "listening on IPv4 address",
            "database system is ready to accept connections",
        )
        if not (started.created and started.started_here and not started.recovered):
            raise CheckFailedError(f"unexpected facts: {started}")
        initdb_phase = marks[Phase.STARTING] - marks[Phase.INITDB]
        total = after_prepare - began
        say(f"start() + prepare(), empty directory:  {seconds(total)}")
        say(
            f"  initdb phase (DACL, password, initdb.exe, rename): "
            f"{seconds(initdb_phase)}"
        )
        say(
            f"    of which initdb.exe:                 "
            f"{seconds(TIMINGS.last('initdb.exe'))}"
        )
        say(
            f"  start (port, conf, pg_ctl, login):     "
            f"{seconds(after_start - marks[Phase.STARTING])}"
        )
        say(
            f"    of which pg_ctl start -w:            "
            f"{seconds(TIMINGS.last('pg_ctl start -w'))}"
        )
        say(
            f"    server, listening -> ready:          {seconds(ready or 0)} (its own "
            f"log)"
        )
        say(
            f"  prepare():                             "
            f"{seconds(after_prepare - after_start)}"
        )
        say(
            f"    CREATE DATABASE:                     "
            f"{seconds(TIMINGS.last('ensure_database'))}"
        )
        say(
            f"    heads (code_revisions, then query):  "
            f"{seconds(TIMINGS.last('code_revisions'))}, "
            f"{seconds(TIMINGS.samples['database_heads'][0])}"
        )
        say(
            f"    migrations, in process, {len(prepared.heads)} head(s):   "
            f"{seconds(TIMINGS.last('upgrade'))} -> {', '.join(prepared.heads)}"
        )
        say(
            f"    facts:                               "
            f"{seconds(TIMINGS.last('check_facts'))}"
        )
        size = sum(p.stat().st_size for p in files.data.rglob("*") if p.is_file())
        say(f"  cluster on disk: {mb(size)}")
        low, high = RFC033A["initdb"]
        say(
            f"  RFC-033a's machine (HDD): initdb {low}-{high} s, listening -> ready "
            f"{RFC033A['listening -> ready'][0]}-{RFC033A['listening -> ready'][1]} s, "
            f"alembic (subprocess) {RFC033A['alembic upgrade head'][0]}-"
            f"{RFC033A['alembic upgrade head'][1]} s"
        )
        for line in same_machine_baselines(storage_of(self.work)):
            say(f"  RFC-033a's script on this machine, {line}")
        cluster.stop()
        self._template = home
        return (
            f"{seconds(total)}: initdb {seconds(TIMINGS.last('initdb.exe'))}, start "
            f"{seconds(after_start - marks[Phase.STARTING])}, CREATE DATABASE "
            f"{seconds(TIMINGS.last('ensure_database'))}, migrations "
            f"{seconds(TIMINGS.last('upgrade'))}"
        )

    # -- 2. how much of initdb is fsync --------------------------------------

    def initdb_fsync(self) -> str:
        base = self.work / "initdb-fsync"
        base.mkdir()
        password = base / "pw"
        password.write_text("diagnostic-only\n", encoding="ascii")
        results: dict[str, list[float]] = {"sync": [], "--no-sync": []}
        for round_number in range(2):
            for label in ("sync", "--no-sync"):
                target = base / f"{label.strip('-')}-{round_number}"
                arguments = [
                    str(self.runtime.initdb),
                    "-D",
                    str(target),
                    "-U",
                    "solidvision",
                    "--auth=scram-sha-256",
                    f"--pwfile={password}",
                    "--encoding=UTF8",
                    "--locale=C",
                ]
                if label == "--no-sync":
                    arguments.append("--no-sync")
                began = time.perf_counter()
                done = subprocess.run(
                    arguments,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    env=self.runtime.child_environment(),
                    creationflags=CREATE_NO_WINDOW,
                )
                elapsed = time.perf_counter() - began
                if done.returncode != 0:
                    raise CheckFailedError(f"initdb {label} exited {done.returncode}")
                results[label].append(elapsed)
                say(f"initdb, {label:<9} run {round_number + 1}: {seconds(elapsed)}")
        with_sync = statistics.mean(results["sync"])
        without = statistics.mean(results["--no-sync"])
        share = 1 - without / with_sync
        say(
            f"fsync is {share:.0%} of initdb here (diagnostic; --no-sync is not an "
            f"option, section 13)"
        )
        return (
            f"sync {seconds(with_sync)}, --no-sync {seconds(without)}: fsync is "
            f"{share:.0%}"
        )

    # -- 3. copying a ready data directory, for the installer ----------------

    def copy_data(self) -> str:
        source = ClusterHome(self.template()).files(MAJOR).data
        files = [p for p in source.rglob("*") if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        times = []
        for index in range(3):
            target = self.work / "copy-data" / str(index)
            began = time.perf_counter()
            shutil.copytree(source, target)
            times.append(time.perf_counter() - began)
        say(f"data\\ of a stopped, migrated cluster: {mb(size)} in {len(files)} files")
        say(f"shutil.copytree, three times (source warm in the cache): {spread(times)}")
        return f"{mb(size)}, {len(files)} files: {spread(times)}"

    # -- 4, 7. warm start and stop -------------------------------------------

    def warm_start(self) -> str:
        home = self.copy_of_template("warm-start")
        cluster = self.cluster(home, timed=True)
        files = ClusterHome(home).files(MAJOR)
        totals, others, ready_times, stops, compares = [], [], [], [], []
        TIMINGS.reset()
        cycles = 10
        for _ in range(cycles):
            began = time.perf_counter()
            started = cluster.start()
            total = time.perf_counter() - began
            parts = (
                TIMINGS.last("runtime check")
                + TIMINGS.last("pg_ctl start -w")
                + TIMINGS.last("connection")
            )
            totals.append(total)
            others.append(total - parts)
            interval = last_interval(
                server_log(files),
                "listening on IPv4 address",
                "database system is ready to accept connections",
            )
            if interval is not None:
                ready_times.append(interval)
            began = time.perf_counter()
            cluster.prepare(started.connection)
            compares.append(time.perf_counter() - began)
            began = time.perf_counter()
            cluster.stop()
            stops.append(time.perf_counter() - began)
        pid_reads = []
        cluster.start()
        for _ in range(200):
            began = time.perf_counter()
            read_postmaster_pid(files.postmaster_pid)
            pid_reads.append(time.perf_counter() - began)
        server = [self.postmaster(home), *children_of(self.postmaster(home))]
        cluster.stop()
        left = [pid for pid in server if PROCESSES.is_alive(pid)]
        say(f"start(), stopped cluster -> connection accepted, {cycles} times:")
        say(f"  total:                    {spread(totals)}")
        say(
            f"  runtime check:            "
            f"{spread(TIMINGS.samples['runtime check'][:cycles])}"
        )
        say(
            f"  pg_ctl start -w:          "
            f"{spread(TIMINGS.samples['pg_ctl start -w'][:cycles])}"
        )
        say(f"    server, listening -> ready: {spread(ready_times)}")
        say(
            f"  connection (login check): "
            f"{spread(TIMINGS.samples['connection'][:cycles])}"
        )
        say(
            f"  the rest (lock, observe, pg_control, port probe, conf, startup.log): "
            f"{spread(others)}"
        )
        say(f"  reading postmaster.pid:   {spread(pid_reads)}")
        say(f"prepare() with nothing to migrate, same process: {spread(compares)}")
        say(
            f"  first call (imports alembic): {seconds(compares[0])}; later calls: "
            f"{spread(compares[1:])}"
        )
        say(f"stop(), fast: {spread(stops)}")
        say(
            f"  of which pg_ctl stop -m fast: "
            f"{spread(TIMINGS.samples['pg_ctl stop -m fast'])}"
        )
        alive = TIMINGS.samples["processes alive when pg_ctl stop returned"]
        say(
            f"  postgres.exe still alive at the instant pg_ctl stop returned: "
            f"{int(min(alive))} to {int(max(alive))} of {len(server)}; all gone "
            f"{spread(TIMINGS.samples['processes gone after pg_ctl stop returned'])} "
            f"later"
        )
        say(
            f"of the {len(server)} postgres.exe of this cluster, alive after the "
            f"last stop: {left or 'none'}"
        )
        if left:
            raise CheckFailedError(f"left behind: {left}")
        self.verdict["stop"] = f"{spread(stops)}; no postgres.exe of the cluster left"
        pg_ctl = statistics.median(TIMINGS.samples["pg_ctl start -w"][:cycles])
        login = statistics.median(TIMINGS.samples["connection"][:cycles])
        ready = seconds(statistics.median(ready_times)) if ready_times else "?"
        return (
            f"{spread(totals)}; pg_ctl -w {seconds(pg_ctl)}, connection "
            f"{seconds(login)}, listening -> ready {ready}"
        )

    # -- 5. adoption ---------------------------------------------------------

    def adoption(self) -> str:
        home = self.copy_of_template("adoption")
        cluster = self.cluster(home)
        cluster.start()
        times = []
        try:
            for _ in range(20):
                began = time.perf_counter()
                adopted = cluster.start()
                times.append(time.perf_counter() - began)
                if adopted.started_here:
                    raise CheckFailedError("an adopted cluster was reported as started")
        finally:
            cluster.stop()
        say(f"start() on a cluster that is up, 20 times: {spread(times)}")
        return spread(times)

    # -- 6. migrations with nothing to migrate -------------------------------

    COLD_SCRIPT = """
import json, sys, time
began = time.perf_counter()
from sqlalchemy import text
from app.infrastructure.database.embedded import database_setup as ds
password = sys.stdin.readline().strip()
connection = ds.ClusterConnection(port=int(sys.argv[1]), password=password)
engine = ds._engine(connection)
with engine.connect() as db:
    db.execute(text("SELECT 1"))
    connected = time.perf_counter()
    if sys.argv[2] == "always":
        ds.upgrade(db)
        db.commit()
    else:
        heads, known = ds.code_revisions()
        found = ds.database_heads(db)
        assert ds.compare_heads(found, heads, known) is ds.HeadComparison.CURRENT
    done = time.perf_counter()
print(json.dumps({"setup": connected - began, "work": done - connected}))
"""

    def noop_migrations(self) -> str:
        home = self.copy_of_template("noop-migrations")
        cluster = self.cluster(home)
        started = cluster.start()
        results: dict[str, list[float]] = {"always": [], "compare": []}
        try:
            for _ in range(5):
                for mode in ("always", "compare"):
                    done = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            self.COLD_SCRIPT,
                            str(started.connection.port),
                            mode,
                        ],
                        cwd=BACKEND,
                        env={**os.environ, "PYTHONPATH": str(BACKEND)},
                        input=started.connection.password + "\n",
                        capture_output=True,
                        text=True,
                    )
                    if done.returncode != 0:
                        raise CheckFailedError(done.stderr.strip()[-300:])
                    results[mode].append(
                        json.loads(done.stdout.splitlines()[-1])["work"]
                    )
        finally:
            cluster.stop()
        say("in a fresh process each time, after the connection is open, 5 times each:")
        say(f"  `alembic upgrade head` with nothing to do: {spread(results['always'])}")
        say(
            f"  comparing heads (ScriptDirectory + one query): "
            f"{spread(results['compare'])}"
        )
        saved = statistics.median(results["always"]) - statistics.median(
            results["compare"]
        )
        say(f"  comparing first saves {seconds(saved)} per start")
        return (
            f"upgrade head {seconds(statistics.median(results['always']))} against "
            f"comparison {seconds(statistics.median(results['compare']))}"
        )

    # -- 8. idle memory ------------------------------------------------------

    def idle_memory(self) -> str:
        home = self.copy_of_template("idle-memory")
        cluster = self.cluster(home)
        cluster.start()
        try:
            time.sleep(5)
            postmaster = self.postmaster(home)
            pids = [postmaster, *children_of(postmaster)]
            working, private = 0, 0
            for pid in pids:
                ws, commit = memory_of(pid)
                working += ws
                private += commit
            say(
                f"{len(pids)} postgres.exe (postmaster + {len(pids) - 1}), ready, idle "
                f"5 s, no client:"
            )
            say(
                f"  sum of working sets:   {mb(working)} (shared memory is counted in "
                f"each)"
            )
            say(f"  sum of private commit: {mb(private)}")
        finally:
            cluster.stop()
        return (
            f"{mb(working)} working set, {mb(private)} private, {len(pids)} processes"
        )

    # -- 11. the facts -------------------------------------------------------

    def facts(self) -> str:
        spec = importlib.util.spec_from_file_location(
            "rfc033a", HERE / "measure_embedded_postgres.py"
        )
        assert spec is not None and spec.loader is not None
        rfc033a = importlib.util.module_from_spec(spec)
        sys.modules["rfc033a"] = rfc033a
        spec.loader.exec_module(rfc033a)
        home = self.copy_of_template("facts")
        cluster = self.cluster(home)
        started = cluster.start()
        try:
            with psycopg.connect(started.connection.conninfo(), autocommit=True) as db:
                found = rfc033a.read_facts(db)
                icu = rfc033a.icu_support(db)
        finally:
            cluster.stop()
        probe = list(rfc033a.COLLATION_PROBE)
        heads, _ = database_setup.code_revisions()
        expected = {
            "major version": "16",
            "minor version": "16.15",
            "pgvector": "0.8.6",
            "encoding": "UTF8",
            "provider, datcollate, datctype": "libc, C, C",
            # C collation by definition: byte order, and ASCII-only lower().
            "ordering": " | ".join(sorted(probe, key=lambda s: s.encode("utf-8"))),
            "lower()": " | ".join(
                "".join(c.lower() if c.isascii() else c for c in s) for s in probe
            ),
            "path outside WIN1252": "byte-identical",
            "hnsw.ef_search": "40",
            "alembic head": ", ".join(sorted(heads)),
        }
        different = []
        for name, value in found.items():
            same = value == expected[name]
            shown = value if len(value) <= 40 else f"{value[:37]}..."
            say(f"{name + ':':<32} {shown:<42} {'OK' if same else 'DIFFERENT'}")
            if not same:
                different.append(name)
        say(f"{'ICU provider in this build:':<32} {icu}")
        if different:
            raise CheckFailedError(f"different from RFC-033a step 8: {different}")
        return f"all {len(found)} fields as RFC-033a step 8 read them"

    # -- 12, 13. killed under write load -------------------------------------

    def _killed_under_load(self, label: str, *, tree: bool) -> str:
        results = []
        ended_total = 0
        for run in range(self.kills):
            home = self.copy_of_template(f"{label}-{run}")
            files = ClusterHome(home).files(MAJOR)
            cluster = self.cluster(home, timed=True)
            started = cluster.start()
            writer = Writer(started.connection)
            writer.start()
            writer.started_writing.wait(timeout=30)
            time.sleep(1.5)
            postmaster = self.postmaster(home)
            before = children_of(postmaster)
            ADAPTER_LOG.messages.clear()
            taskkill(postmaster, tree=tree)
            # Who is still there the instant the kill returns, and what each
            # one is: read in the background, so that the start below is still
            # the immediate restart being measured.
            survivors = [
                pid for pid in children_of(postmaster) if PROCESSES.is_alive(pid)
            ]
            roles: dict[int, str] = {}
            asking = threading.Thread(
                target=lambda found=survivors: roles.update(command_lines(found)),
                daemon=True,
            )
            if survivors:
                asking.start()
            phases: list[Phase] = []
            TIMINGS.reset()
            began = time.perf_counter()
            recovered = cluster.start(on_phase=phases.append)
            start_time = time.perf_counter() - began
            pg_ctl = sum(TIMINGS.samples.get("pg_ctl start -w", [0.0]))
            writer.join(timeout=30)
            present, wanted = confirmed_rows(recovered.connection, writer.confirmed)
            recovery = last_interval(
                server_log(files),
                "database system was interrupted",
                "database system is ready to accept connections",
            )
            ended = [
                int(found.group(1))
                for found in (
                    re.match(r"Ending PID (\d+)", message)
                    for message in ADAPTER_LOG.messages
                )
                if found
            ]
            ended_total += len(ended)
            cluster.stop()
            if survivors:
                asking.join(timeout=30)
            say(
                f"run {run + 1}: {writer.confirmed + 1} commits confirmed, "
                f"{len(before)} children; start() {seconds(start_time)} "
                f"(pg_ctl {seconds(pg_ctl)}), "
                f"recovered={recovered.recovered}, server recovery "
                f"{seconds(recovery) if recovery is not None else '?'}, "
                f"rows {present}/{wanted}, orphans ended by the adapter: {len(ended)}"
            )
            if survivors:
                born = [pid for pid in survivors if pid not in before]
                say(
                    f"  alive the instant the kill returned: {len(survivors)} "
                    f"child(ren), of which {len(born)} had been created during "
                    f"the kill itself"
                )
                for pid in survivors:
                    marks = [
                        "born during the kill" if pid in born else "older",
                        "ENDED by the adapter" if pid in ended else "left by itself",
                    ]
                    say(
                        f"    PID {pid} ({', '.join(marks)}): "
                        f"{roles.get(pid, 'gone before it could be asked')[-70:]}"
                    )
            if present != wanted:
                raise CheckFailedError(f"{wanted - present} confirmed commits lost")
            if not (recovered.recovered and recovered.started_here):
                raise CheckFailedError(f"unexpected facts: {recovered}")
            if Phase.RECOVERING not in phases:
                raise CheckFailedError("on_phase never said `recovering`")
            results.append((start_time, recovery or 0.0, writer.confirmed + 1))
        starts = [r[0] for r in results]
        recoveries = [r[1] for r in results]
        return (
            f"{len(results)} of {self.kills} recovered without intervention, no "
            f"confirmed commit lost ({sum(r[2] for r in results)} in all); start() "
            f"{spread(starts)}, server recovery {spread(recoveries)}; orphans the "
            f"adapter had to end: {ended_total}"
        )

    def killed_postmaster(self) -> str:
        return self._killed_under_load("killed-postmaster", tree=False)

    def killed_tree(self) -> str:
        return self._killed_under_load("killed-tree", tree=True)

    # -- 14. one backend killed ----------------------------------------------

    def killed_backend(self) -> str:
        home = self.copy_of_template("killed-backend")
        files = ClusterHome(home).files(MAJOR)
        cluster = self.cluster(home)
        started = cluster.start()
        try:
            writer = Writer(started.connection)
            writer.start()
            writer.started_writing.wait(timeout=30)
            time.sleep(1.0)
            postmaster = self.postmaster(home)
            taskkill(writer.backend_pid)
            killed = time.perf_counter()
            writer.join(timeout=30)
            seen: list[str] = []
            refusals: dict[str, int] = {}
            while True:
                status = cluster.status().status.value
                if not seen or seen[-1] != status:
                    seen.append(status)
                try:
                    with psycopg.connect(
                        started.connection.conninfo("postgres"), autocommit=True
                    ) as db:
                        db.execute("SELECT 1")
                    break
                except psycopg.Error as error:
                    reason = str(error).strip().splitlines()[-1][-60:]
                    refusals[reason] = refusals.get(reason, 0) + 1
                    if time.perf_counter() - killed > 60:
                        raise CheckFailedError(
                            "no connection 60 s after the kill"
                        ) from None
                    time.sleep(0.02)
            back = time.perf_counter() - killed
            reinitialized = last_interval(
                server_log(files),
                "all server processes terminated; reinitializing",
                "database system is ready to accept connections",
            )
            present, wanted = confirmed_rows(started.connection, writer.confirmed)
            same_postmaster = self.postmaster(
                home
            ) == postmaster and PROCESSES.is_alive(postmaster)
            log = server_log(files)
            say(f"backend {writer.backend_pid} killed with {wanted} commits confirmed")
            say(
                f"  the postmaster ({postmaster}) is the same and alive: "
                f"{same_postmaster}"
            )
            say(
                f"  next connection accepted {seconds(back)} after the kill; the "
                f"server's own reinitialization took "
                f"{seconds(reinitialized) if reinitialized is not None else '?'}"
            )
            say(
                f"  status() while connections were being refused: "
                f"{' -> '.join(seen)} (postmaster.pid is not rewritten for this)"
            )
            for reason, count in refusals.items():
                say(f"  refused {count} time(s): ...{reason}")
            say(
                f"  server said 'terminating any other active server processes': "
                f"{'terminating any other active server processes' in log}"
            )
            say(
                f"  server said 'all server processes terminated; reinitializing': "
                f"{'all server processes terminated; reinitializing' in log}"
            )
            say(
                f"  rows {present}/{wanted}; adapter status now: "
                f"{cluster.status().status.value}"
            )
            if not same_postmaster or present != wanted:
                raise CheckFailedError("the cluster did not survive a killed backend")
            if cluster.status().status is not ClusterStatus.READY:
                raise CheckFailedError("status is not ready")
        finally:
            self.stop_quietly(cluster)
        return (
            f"cluster stayed up (same postmaster); next connection after "
            f"{seconds(back)}, with status() saying {'/'.join(seen)} throughout; "
            f"no confirmed commit lost"
        )

    # -- 15. immediate -------------------------------------------------------

    def immediate(self) -> str:
        home = self.copy_of_template("immediate")
        files = ClusterHome(home).files(MAJOR)
        cluster = self.cluster(home)
        started = cluster.start()
        writer = Writer(started.connection)
        writer.start()
        writer.started_writing.wait(timeout=30)
        time.sleep(1.0)
        subprocess.run(
            [
                str(self.runtime.pg_ctl),
                "stop",
                "-m",
                "immediate",
                "-w",
                "-D",
                str(files.data),
            ],
            env=self.runtime.child_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        writer.join(timeout=30)
        lock_left = files.postmaster_pid.exists()
        began = time.perf_counter()
        recovered = cluster.start()
        elapsed = time.perf_counter() - began
        present, wanted = confirmed_rows(recovered.connection, writer.confirmed)
        cluster.stop()
        say(
            f"pg_ctl stop -m immediate with {wanted} commits confirmed; postmaster.pid "
            f"left behind: {lock_left}"
        )
        say(
            f"start(): {seconds(elapsed)}, recovered={recovered.recovered}, rows "
            f"{present}/{wanted}"
        )
        if not recovered.recovered or present != wanted:
            raise CheckFailedError("immediate shutdown was not recovered cleanly")
        return (
            f"recovered in {seconds(elapsed)} (recovered=True, known from pg_control, "
            f"no lock file left); no confirmed commit lost"
        )

    # -- 16. orphans ---------------------------------------------------------

    def orphans(self) -> str:
        # How long the children outlive a killed postmaster, on their own.
        lives = []
        log_free = []
        for run in range(5):
            home = self.copy_of_template(f"orphan-life-{run}")
            files = ClusterHome(home).files(MAJOR)
            cluster = self.cluster(home)
            cluster.start()
            postmaster = self.postmaster(home)
            before = children_of(postmaster)
            taskkill(postmaster)
            killed = time.perf_counter()
            free_at = None
            while True:
                elapsed = time.perf_counter() - killed
                if free_at is None:
                    try:
                        with files.startup_log.open("ab"):
                            free_at = elapsed
                    except PermissionError:
                        pass
                if not any(PROCESSES.is_alive(pid) for pid in before):
                    lives.append(time.perf_counter() - killed)
                    break
                if elapsed > 30:
                    raise CheckFailedError("children alive 30 s after the postmaster")
                time.sleep(0.001)
            log_free.append(free_at if free_at is not None else lives[-1])
            cluster.start()
            cluster.stop()
        say(
            f"taskkill /F on the postmaster, idle cluster, 5 times ({len(before)} "
            f"children):"
        )
        say(f"  last child gone after:        {spread(lives)}")
        say(f"  startup.log writable after:   {spread(log_free)}")

        # An orphan that does not leave, and two decoys that must survive.
        other_runtime = self.runtime_copy()
        decoy_a = self.cluster(
            self.copy_of_template("decoy-other-runtime"), runtime=other_runtime
        )
        decoy_b = self.cluster(self.copy_of_template("decoy-same-runtime"))
        main_home = self.copy_of_template("orphan-suspended")
        main = self.cluster(main_home)
        try:
            a = decoy_a.start()
            b = decoy_b.start()
            main.start()
            decoy_pids = {
                "another runtime's cluster": [
                    self.postmaster(self.home("decoy-other-runtime"))
                ],
                "another cluster of this runtime": [
                    self.postmaster(self.home("decoy-same-runtime"))
                ],
            }
            for pids in decoy_pids.values():
                pids.extend(children_of(pids[0]))
            postmaster = self.postmaster(main_home)
            victim = children_of(postmaster)[0]
            suspend(victim)
            taskkill(postmaster)
            time.sleep(1.0)
            survivors = [
                pid for pid in children_of(postmaster) if PROCESSES.is_alive(pid)
            ]
            ADAPTER_LOG.messages.clear()
            began = time.perf_counter()
            recovered = main.start()
            elapsed = time.perf_counter() - began
            ended = [m for m in ADAPTER_LOG.messages if m.startswith("Ending PID")]
            say(
                f"one child suspended (PID {victim}) so that it outlives the "
                f"postmaster; alive 1 s after the kill: {survivors}"
            )
            say(f"  start(): {seconds(elapsed)}, recovered={recovered.recovered}")
            for message in ended:
                say(f"  adapter: {message}")
            say(f"  the suspended orphan is alive: {PROCESSES.is_alive(victim)}")
            intact = {}
            for name, pids in decoy_pids.items():
                intact[name] = all(PROCESSES.is_alive(pid) for pid in pids)
                say(
                    f"  decoy, {name}: {len(pids)} processes, all alive: {intact[name]}"
                )
            for name, connection in (
                ("other runtime", a.connection),
                ("same runtime", b.connection),
            ):
                with psycopg.connect(connection.conninfo("postgres")) as db:
                    db.execute("SELECT 1")
                say(f"  decoy ({name}) still answers queries")
            if (
                PROCESSES.is_alive(victim)
                or not all(intact.values())
                or len(ended) != 1
            ):
                raise CheckFailedError(
                    "the orphan rule ended the wrong set of processes"
                )

            # Section 8.4: this runtime will not adopt the other one's cluster.
            try:
                self.cluster(self.home("decoy-other-runtime")).start()
                raise CheckFailedError("adopted a cluster held by another runtime")
            except ClusterRefusedError as refusal:
                say(
                    f"  adopting the other runtime's cluster: refused -- "
                    f"{str(refusal)[:150]}"
                )
                named = str(other_runtime.postgres) in str(refusal)
                say(f"  the refusal names the runtime holding it: {named}")
                if not named:
                    raise CheckFailedError(
                        "the refusal does not name the runtime"
                    ) from None
        finally:
            for cluster in (main, decoy_a, decoy_b):
                self.stop_quietly(cluster)
        return (
            f"children gone {spread(lives)} after the kill; a suspended orphan was "
            f"ended after the grace ({seconds(elapsed)}), both decoys survived"
        )

    # -- 17. initdb interrupted ----------------------------------------------

    def initdb_interrupted(self) -> str:
        results = []
        for label, tree in (
            ("the whole tree (taskkill /F /T)", True),
            ("the caller alone (taskkill /F)", False),
        ):
            home = self.home(f"initdb-interrupted-{'tree' if tree else 'alone'}")
            files = ClusterHome(home).files(MAJOR)
            # The base interpreter, not the venv's launcher: the launcher puts
            # its child in a job that dies with it, which would turn "the
            # caller alone" into "the whole tree".
            base = getattr(sys, "_base_executable", sys.executable)
            site = [p for p in sys.path if p.endswith("site-packages")]
            environment = {
                **os.environ,
                "PYTHONPATH": os.pathsep.join([str(BACKEND), *site]),
            }
            process = subprocess.Popen(
                [base, "-c", ADAPTER_CHILD, str(self.runtime.root), str(home), "start"],
                cwd=BACKEND,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.perf_counter() + 60
            while not files.data_initdb.exists():
                if time.perf_counter() > deadline:
                    raise CheckFailedError("data.initdb never appeared")
                time.sleep(0.01)
            time.sleep(2.0)
            taskkill(process.pid, tree=tree)
            process.wait(timeout=30)
            time.sleep(0.3)
            running = [
                name
                for _, name in runtime_processes(self.runtime)
                if name == "initdb.exe"
            ]
            say(f"killed {label}, 2.0 s into initdb:")
            say(
                f"  data.initdb exists: {files.data_initdb.exists()}, data exists: "
                f"{files.data.exists()}, an initdb.exe still running: {bool(running)}"
            )
            cluster = self.cluster(home)
            began = time.perf_counter()
            started = cluster.start()
            elapsed = time.perf_counter() - began
            with psycopg.connect(started.connection.conninfo("postgres")) as db:
                db.execute("SELECT 1")
            cluster.stop()
            say(
                f"  start(): {seconds(elapsed)}, created={started.created}; "
                f"data.initdb left: {files.data_initdb.exists()}"
            )
            if not started.created or files.data_initdb.exists():
                raise CheckFailedError("the interrupted initdb was not cleaned up")
            results.append(f"{'tree' if tree else 'caller alone'} {seconds(elapsed)}")
        return (
            f"data.initdb deleted and the cluster recreated, both ways: "
            f"{', '.join(results)}"
        )

    # -- 18. a start killed during recovery ----------------------------------

    def killed_in_recovery(self) -> str:
        home = self.copy_of_template("killed-in-recovery")
        files = ClusterHome(home).files(MAJOR)
        cluster = self.cluster(home)
        started = cluster.start()
        with psycopg.connect(
            started.connection.conninfo("postgres"), autocommit=True
        ) as db:
            db.execute("CREATE TABLE rfc033_bulk (n integer, filler text)")
            db.execute(
                "INSERT INTO rfc033_bulk SELECT g, repeat('x', 200) "
                "FROM generate_series(1, 600000) g"
            )
            row = db.execute(
                "SELECT count(*), pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), "
                "(SELECT redo_lsn FROM pg_control_checkpoint()))) FROM rfc033_bulk"
            ).fetchone()
        assert row is not None
        rows, wal = int(row[0]), str(row[1])
        first = self.postmaster(home)
        taskkill(first, tree=True)
        time.sleep(0.5)
        process = self.child(home, "start")
        second = None
        deadline = time.perf_counter() + 60
        while time.perf_counter() < deadline:
            pid_file = read_postmaster_pid(files.postmaster_pid)
            if (
                pid_file is not None
                and pid_file.pid != first
                and pid_file.status == "starting"
                and PROCESSES.is_alive(pid_file.pid)
            ):
                second = pid_file.pid
                taskkill(second, tree=True)
                break
            time.sleep(0.002)
        outcome = self.child_result(process, timeout=120)
        if second is None:
            raise CheckFailedError(f"never saw the server in `starting`: {outcome}")
        say(
            f"{rows} rows committed, {wal} of WAL since the last checkpoint; "
            f"postmaster killed"
        )
        say(
            f"  second postmaster (PID {second}) killed while its lock file said "
            f"`starting`"
        )
        say(
            f"  the start() that was waiting for it: "
            f"{str(outcome.get('error', outcome))[:160]}"
        )
        began = time.perf_counter()
        recovered = cluster.start()
        elapsed = time.perf_counter() - began
        with psycopg.connect(recovered.connection.conninfo("postgres")) as db:
            found = db.execute("SELECT count(*) FROM rfc033_bulk").fetchone()
        cluster.stop()
        assert found is not None
        say(
            f"  next start(): {seconds(elapsed)}, recovered={recovered.recovered}, "
            f"rows {found[0]}/{rows}"
        )
        if not recovered.recovered or int(found[0]) != rows:
            raise CheckFailedError("the second recovery lost data")
        return (
            f"recovered in {seconds(elapsed)} after a recovery was itself killed "
            f"({wal} of WAL); {rows} rows intact"
        )

    # -- what `pg_ctl start -w` can be trusted for (section 8.3) --------------

    def pg_ctl_stale_lock(self) -> str:
        """`pg_ctl` itself, not the adapter, over a lock file left by a kill.

        On Windows `pg_ctl` cannot match `postmaster.pid` to the process it
        started. What it starts is a `cmd.exe`, and what it checks is how
        old the file's start time is.
        """
        fooled, honest = [], []
        for index, delay in enumerate((0.0, 0.5, 2.0, 5.0)):
            home = self.copy_of_template(f"stale-lock-{index}")
            files = ClusterHome(home).files(MAJOR)
            cluster = self.cluster(home)
            started = cluster.start()
            postmaster = self.postmaster(home)
            if index == 0:
                parent = next(
                    p.parent_pid for p in PROCESSES.snapshot() if p.pid == postmaster
                )
                executable = PROCESSES.executable(parent)
                line = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f'(Get-CimInstance Win32_Process -Filter "ProcessId={parent}")'
                        ".CommandLine",
                    ],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                say(
                    f"the postmaster's parent is "
                    f"{executable.name if executable else parent}, started as:"
                )
                say(f"  {line.replace(str(home), '<home>')}")
            time.sleep(delay)
            server = [postmaster, *children_of(postmaster)]
            taskkill(postmaster, tree=True)
            deadline = time.perf_counter() + 30
            while any(PROCESSES.is_alive(pid) for pid in server):
                if time.perf_counter() > deadline:
                    raise CheckFailedError("the killed server's processes stayed")
                time.sleep(0.005)
            stale = files.postmaster_pid.read_text(errors="replace").splitlines()
            age = time.time() - int(stale[2])
            began = time.perf_counter()
            with files.pg_ctl_log.open("wb") as output:
                returncode = subprocess.run(
                    [
                        str(self.runtime.pg_ctl),
                        "start",
                        "-w",
                        "-t",
                        "60",
                        "-D",
                        str(files.data),
                        "-l",
                        str(files.startup_log),
                    ],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=self.runtime.child_environment(),
                    creationflags=CREATE_NO_WINDOW,
                ).returncode
            waited = time.perf_counter() - began
            pid_file = read_postmaster_pid(files.postmaster_pid)
            if pid_file is None or pid_file.pid == postmaster:
                seen = "still the dead server's file, saying `ready`"
            else:
                seen = f"the new server's file, saying `{pid_file.status}`"
            try:
                database_setup.verify_login(started.connection)
                login = "accepted"
            except EmbeddedPostgresError as error:
                login = f"refused ({str(error).rsplit(':', 1)[-1].strip()})"
            was_fooled = login != "accepted"
            (fooled if was_fooled else honest).append(age)
            say(
                f"lock file {age:.1f} s old when pg_ctl started: pg_ctl start -w "
                f"returned {returncode} after {seconds(waited)}; postmaster.pid at "
                f"that instant: {seen}; a login at that instant: {login}"
            )
            cluster.start()
            cluster.stop()
        if not fooled:
            return "pg_ctl was not fooled by a stale lock file in these runs"
        waited_for = f"{min(honest):.1f}-{max(honest):.1f} s old" if honest else "never"
        return (
            f"pg_ctl start -w returned success with the server not ready when the "
            f"dead server's lock file was {min(fooled):.1f}-{max(fooled):.1f} s old, "
            f"and waited properly when it was {waited_for}"
        )

    # -- what the runtime's own client leaves behind --------------------------

    def runtime_psql(self) -> str:
        """`psql.exe` from the runtime, which the adapter never runs.

        RFC-033a's script does, and after its cluster stopped a
        `ccapiserver.exe` of the runtime's Kerberos package was still running
        and holding the runtime's files. This asks which client setting
        starts it. Run against a copy of the runtime, so that what it leaves
        is this run's to end.
        """
        copy = self.runtime_copy()
        home = self.copy_of_template("runtime-psql")
        files = ClusterHome(home).files(MAJOR)
        cluster = self.cluster(home, runtime=copy)
        started = cluster.start()

        def from_the_copy() -> list[str]:
            names = []
            for process in PROCESSES.snapshot():
                executable = PROCESSES.executable(process.pid)
                if executable is not None and copy.contains(executable):
                    if executable.name.lower() != "postgres.exe":
                        names.append(executable.name)
            return sorted(names)

        outcomes = {}
        try:
            for label, extra in (
                ("PGGSSENCMODE=disable", {"PGGSSENCMODE": "disable"}),
                ("libpq's defaults", {}),
            ):
                environment = copy.child_environment()
                environment["PGPASSWORD"] = files.password.read_text().strip()
                environment.update(extra)
                done = subprocess.run(
                    [
                        str(copy.bin_directory / "psql.exe"),
                        "-X",
                        "-q",
                        "-A",
                        "-t",
                        "-h",
                        "127.0.0.1",
                        "-p",
                        str(started.connection.port),
                        "-U",
                        "solidvision",
                        "-d",
                        "postgres",
                        "-c",
                        "SELECT 1",
                    ],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    env=environment,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=120,
                )
                time.sleep(1.0)
                outcomes[label] = from_the_copy()
                say(
                    f"psql.exe, {label}: exit {done.returncode}; other processes of "
                    f"the runtime afterwards: {outcomes[label] or 'none'}"
                )
        finally:
            cluster.stop()
        time.sleep(1.0)
        left = from_the_copy()
        say(
            f"after the cluster stopped, still running from the runtime: "
            f"{left or 'none'}"
        )
        # Whatever it is, it is this run's: end it, or the copy cannot be removed.
        for process in PROCESSES.snapshot():
            executable = PROCESSES.executable(process.pid)
            if executable is not None and copy.contains(executable):
                taskkill(process.pid, tree=True)
        if not left:
            return "psql.exe from the runtime left nothing running"
        return (
            f"psql.exe from the runtime left {', '.join(left)} running after the "
            f"cluster stopped (with PGGSSENCMODE=disable: "
            f"{', '.join(outcomes['PGGSSENCMODE=disable']) or 'nothing'}); the adapter "
            f"never runs psql"
        )

    # -- 19. busy port -------------------------------------------------------

    def busy_port(self) -> str:
        home = self.copy_of_template("busy-port")
        files = ClusterHome(home).files(MAJOR)
        cluster = self.cluster(home)
        last = cluster.start().connection.port
        cluster.stop()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
            squatter.bind(("127.0.0.1", last))
            squatter.listen()
            refusal = "none"
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                try:
                    second.bind(("127.0.0.1", last))
                except OSError as error:
                    refusal = f"winerror {getattr(error, 'winerror', error.errno)}"
            began = time.perf_counter()
            started = cluster.start()
            elapsed = time.perf_counter() - began
            pid_file = read_postmaster_pid(files.postmaster_pid)
            with psycopg.connect(started.connection.conninfo("postgres")) as db:
                server = db.execute("SELECT inet_server_port()").fetchone()
        cluster.stop()
        assert pid_file is not None and server is not None
        say(f"last port {last} held by a listening socket; start(): {seconds(elapsed)}")
        say(
            f"  a second bind on it is refused with: {refusal} (WSAEADDRINUSE is 10048)"
        )
        say(
            f"  new port {started.connection.port}; postmaster.pid says "
            f"{pid_file.port}; the server says {server[0]}"
        )
        # The port in the .env, when something answers on it: Docker publishes
        # the development database on 0.0.0.0, not on 127.0.0.1.
        from app.infrastructure.config.settings import Settings

        fixed = Settings().database_port
        try:
            socket.create_connection(("127.0.0.1", fixed), timeout=3).close()
            answered = True
        except OSError:
            answered = False
        if answered:
            say(
                f"  the .env's port, {fixed}, answers on 127.0.0.1 right now; what "
                f"the probe says of a bind on 127.0.0.1:{fixed}: "
                f"usable={PortProber().usable(fixed)}"
            )
        else:
            say(f"  nothing answers on the .env's port, {fixed}")
        if started.connection.port == last or pid_file.port != server[0]:
            raise CheckFailedError(
                "the port was not moved, or not read from postmaster.pid"
            )
        return (
            f"moved from {last} to {started.connection.port}, read from "
            f"postmaster.pid, in {seconds(elapsed)}"
        )

    # -- 20. reserved range --------------------------------------------------

    def reserved_range(self) -> str:
        listing = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True,
            text=True,
            encoding="oem",
            errors="replace",
        ).stdout
        ranges = [
            (int(a), int(b), "*" in rest)
            for a, b, rest in re.findall(
                r"^\s*(\d+)\s+(\d+)(.*)$", listing, re.MULTILINE
            )
        ]
        say(f"netsh ... show excludedportrange: {len(ranges)} ranges")
        say(
            "  " + ", ".join(f"{a}-{b}{'*' if admin else ''}" for a, b, admin in ranges)
        )
        # Section 8.2, step 3: does port 0 ever hand out a reserved port?
        asked = 2000
        handed = [PortProber().free_port() for _ in range(asked)]
        inside = [
            port
            for port in handed
            if any(first <= port <= last for first, last, _ in ranges)
        ]
        say(
            f"bind to port 0, {asked} times: {len(set(handed))} distinct ports from "
            f"{min(handed)} to {max(handed)}, of which inside a reserved range: "
            f"{len(inside)}"
        )
        refused = None
        # The widest first: Hyper-V, WSL and Docker reserve blocks of a hundred.
        for first, last, _ in sorted(ranges, key=lambda r: r[0] - r[1]):
            port = (first + last) // 2
            if port < 1024:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as error:
                    if getattr(error, "winerror", None) == 10013:
                        refused = port
                        break
        if refused is None:
            say(
                "no range on this machine refuses a bind: nothing to force the last "
                "port into"
            )
            return (
                "no reserved range that refuses a bind on this machine: not exercised"
            )
        say(f"port {refused} refuses bind with WSAEACCES (10013), not WSAEADDRINUSE")
        say(f"  the probe's answer for it: usable={PortProber().usable(refused)}")
        home = self.copy_of_template("reserved-range")
        files = ClusterHome(home).files(MAJOR)
        files.server_conf.write_text(f"port = {refused}\n", encoding="utf-8")
        cluster = self.cluster(home)
        started = cluster.start()
        cluster.stop()
        say(f"  last port forced to {refused}; start() chose {started.connection.port}")
        if started.connection.port == refused:
            raise CheckFailedError("the server was started inside the reserved range")

        # What the server itself says when the probe is not there to stop it.
        class ProbeThatSaysYes(PortProber):
            def usable(self, port: int) -> bool:
                return True

        files.server_conf.write_text(f"port = {refused}\n", encoding="utf-8")
        marks = {p: p.stat().st_size for p in files.server_log_directory.iterdir()}
        raced = self.cluster(home, ports=ProbeThatSaysYes())
        again = raced.start()
        raced.stop()
        said = []
        for path, size in marks.items():
            with path.open("rb") as handle:
                handle.seek(size)
                said += [
                    line
                    for line in handle.read().decode("utf-8", "replace").splitlines()
                    if "could not" in line
                ]
        say(
            f"  with the probe bypassed, the server was sent to {refused} and said, in "
            f"its own log:"
        )
        for line in said[:3]:
            say(f"    {line}")
        say(
            f"  startup.log held the refusal: "
            f"{'could not bind' in files.startup_log.read_text(errors='replace')}"
        )
        say(f"  the adapter recognized it and started on {again.connection.port}")
        if again.connection.port == refused or not said:
            raise CheckFailedError(
                "the bind refusal was not recognized from the server's log"
            )
        return (
            f"{len(ranges)} ranges; port {refused} refused by the probe (WSAEACCES) "
            f"and, "
            f"bypassing it, by the server; both times the start moved to another port"
        )

    # -- 21. two starts at once ----------------------------------------------

    def two_starts(self) -> str:
        home = self.home("two-starts")
        began = time.perf_counter()
        callers = [self.child(home, "start") for _ in range(2)]
        outcomes = [self.child_result(caller) for caller in callers]
        elapsed = time.perf_counter() - began
        cluster = self.cluster(home)
        postmasters = [
            name
            for _, name in runtime_processes(self.runtime)
            if name == "postgres.exe"
        ]
        top = {o.get("postmaster") for o in outcomes}
        cluster.stop()
        for index, outcome in enumerate(outcomes):
            say(f"caller {index + 1}: {outcome}")
        say(
            f"both returned after {seconds(elapsed)}; one postmaster PID between them: "
            f"{len(top) == 1}; postgres.exe processes then: {len(postmasters)}"
        )
        created = sorted(bool(o.get("created")) for o in outcomes)
        owners = sorted(bool(o.get("started_here")) for o in outcomes)
        if created != [False, True] or owners != [False, True] or len(top) != 1:
            raise CheckFailedError(f"not one creator and one adopter: {outcomes}")
        return (
            f"one created and started, the other waited on the lock and adopted; one "
            f"postmaster ({seconds(elapsed)})"
        )

    # -- 22. an elevated caller ----------------------------------------------

    def elevated(self) -> str:
        elevated = bool(is_elevated())
        if not elevated:
            say("this process is not elevated, and elevating needs a UAC prompt.")
            say("MANUAL: from an elevated PowerShell, in the repository root:")
            say(
                f"  {sys.executable} {Path(__file__).relative_to(REPO)} --only elevated"
            )
            say("the same readings without elevation, for the comparison:")
        home = self.copy_of_template("elevated")
        cluster = self.cluster(home)
        started = cluster.start()
        try:
            postmaster = self.postmaster(home)
            say(
                f"this process is elevated: {elevated}; start() worked on port "
                f"{started.connection.port}"
            )
            say(f"  the postmaster's token is elevated: {is_elevated(postmaster)}")
            caller = token_facts()
            server = token_facts(postmaster)
            say(f"  this process's token: {caller}")
            say(f"  the postmaster's token: {server}")
            parent = next(
                (p.parent_pid for p in PROCESSES.snapshot() if p.pid == postmaster),
                None,
            )
            if parent:
                executable = PROCESSES.executable(parent)
                say(
                    f"  its parent's token "
                    f"({executable.name if executable else parent}, which pg_ctl "
                    f"created): {token_facts(parent)}"
                )
            for child in children_of(postmaster)[:1]:
                say(f"  one of its children's token: {token_facts(child)}")
            control = postgres_run_directly(self.runtime, home / "no-such-directory")
            say(
                f"  control, postgres.exe run directly by this process, on a "
                f"directory that does not exist: {control}"
            )
            with psycopg.connect(started.connection.conninfo("postgres")) as db:
                db.execute("SELECT 1")
        finally:
            cluster.stop()
        # `TokenElevation` reads True on the server either way (measured), so
        # the verdict is the tokens side by side and what the control said.
        brief = "elevation type="
        if not elevated:
            return (
                f"MANUAL: not run (this process is not elevated). Without "
                f"elevation, caller's token: {caller.partition(brief)[2]}; "
                f"postmaster's: {server.partition(brief)[2]}"
            )
        return (
            f"started from an elevated process. postgres.exe run directly: "
            f"{control[:90]}. Caller's token: {caller.partition(brief)[2]}. "
            f"Postmaster's: {server.partition(brief)[2]}"
        )

    # -- 23. a caller without a console --------------------------------------

    NO_CONSOLE_SCRIPT = """
import json, subprocess, sys, time
from pathlib import Path
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.runtime import PostgresRuntime

runtime = PostgresRuntime(Path(sys.argv[1]))
home = ClusterHome(Path(sys.argv[2]))
files = home.files(16)
result = {}
try:
    if sys.argv[4] == "adapter":
        started = EmbeddedPostgres(runtime, home).start()
        result["port"] = started.connection.port
    else:
        with open(files.pg_ctl_log, "wb") as output:
            done = subprocess.run(
                [str(runtime.pg_ctl), "start", "-w", "-D", str(files.data),
                 "-l", str(files.startup_log)],
                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=runtime.child_environment(),
            )
        result["pg_ctl"] = done.returncode
    time.sleep(1.5)
except Exception as error:
    result["error"] = repr(error)
Path(sys.argv[3]).write_text(json.dumps(result), encoding="utf-8")
"""

    def _from_pythonw(
        self, home: Path, mode: str
    ) -> tuple[Any, dict[int, tuple[str, str, int]]]:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            raise CheckFailedError(f"{pythonw} does not exist")
        result_file = home.parent / f"result-{mode}.json"
        before = visible_windows()
        process = subprocess.Popen(
            [
                str(pythonw),
                "-c",
                self.NO_CONSOLE_SCRIPT,
                str(self.runtime.root),
                str(home),
                str(result_file),
                mode,
            ],
            cwd=BACKEND,
            env={**os.environ, "PYTHONPATH": str(BACKEND)},
        )
        seen: dict[int, tuple[str, str, int]] = {}
        while process.poll() is None:
            seen.update(visible_windows())
            time.sleep(0.02)
        seen.update(visible_windows())
        new = {handle: info for handle, info in seen.items() if handle not in before}
        result = (
            json.loads(result_file.read_text(encoding="utf-8"))
            if result_file.exists()
            else {}
        )
        return result, new

    def no_console(self) -> str:
        home = self.copy_of_template("no-console")
        cluster = self.cluster(home)
        result, new = self._from_pythonw(home, "adapter")
        consoles = console_windows(new)
        cluster.stop()
        say(f"start() from pythonw.exe (no console): {result}")
        say(
            f"  new visible top-level windows while it ran: {len(new)}, of which "
            f"console windows: {len(consoles)}"
        )
        for line in describe_windows(new):
            say(f"    {line}")
        self.window_notes += describe_windows(new)

        control_home = self.copy_of_template("no-console-control")
        control = self.cluster(control_home)
        control_result, control_new = self._from_pythonw(control_home, "control")
        control_consoles = console_windows(control_new)
        self.stop_quietly(control)
        say(f"control, the same pg_ctl without CREATE_NO_WINDOW: {control_result}")
        say(f"  new console windows: {len(control_consoles)}")
        for line in describe_windows(control_consoles):
            say(f"    {line}")
        if "error" in result or consoles:
            raise CheckFailedError("a console window appeared, or the start failed")
        proof = (
            "the control opened one"
            if control_consoles
            else "the control opened none either: the method did not detect it"
        )
        return f"no window ({len(new)} new windows, none a console); {proof}"

    # -- 24. Ctrl+C ----------------------------------------------------------

    CTRL_C_SCRIPT = """
import ctypes, json, os, subprocess, sys, time
from pathlib import Path
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.postmaster_pid import read_postmaster_pid
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.database.embedded.windows_processes import WindowsProcesses

mode, runtime_root, home_root, result_file, backend = sys.argv[1:6]
runtime = PostgresRuntime(Path(runtime_root))
home = ClusterHome(Path(home_root))
files = home.files(16)
cluster = EmbeddedPostgres(runtime, home)
kernel32 = ctypes.WinDLL("kernel32")
processes = WindowsProcesses()
result = {"mode": mode}
# "Ignore Ctrl+C" is a per-process flag that children inherit, and a process
# started by a tool runner usually has it set. Restore normal processing
# before anything is started here, or no child would ever see the event.
kernel32.SetConsoleCtrlHandler(None, False)

def ctrl_c():
    kernel32.SetConsoleCtrlHandler(None, True)   # this process ignores it
    # ... and everyone else on this console gets it.
    result["event_sent"] = bool(kernel32.GenerateConsoleCtrlEvent(0, 0))

try:
    if mode in ("adapter", "shared"):
        if mode == "adapter":
            cluster.start()
        else:
            with open(files.pg_ctl_log, "wb") as output:
                subprocess.run(
                    [str(runtime.pg_ctl), "start", "-w", "-D", str(files.data),
                     "-l", str(files.startup_log)],
                    stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env=runtime.child_environment(),
                )
        postmaster = read_postmaster_pid(files.postmaster_pid).pid
        ctrl_c()
        time.sleep(4)
        result["postmaster_alive"] = processes.is_alive(postmaster)
        result["status"] = cluster.status().status.value
    else:
        marker = Path(result_file).with_suffix(".marker")
        child = (
            "import sys, time\\n"
            "try:\\n"
            "    time.sleep(120)\\n"
            "except KeyboardInterrupt:\\n"
            f"    open(r'{marker}', 'w').write(str(time.time()))\\n"
            "    sys.exit(42)\\n"
        )
        environment = dict(os.environ, EMBEDDED_POSTGRES_DIRECTORY=home_root)
        errors = Path(result_file).with_suffix(".stderr")
        with open(errors, "w") as stderr:
            run = subprocess.Popen(
                [sys.executable, "-m", "app.infrastructure.database.embedded",
                 "run", "--", sys.executable, "-c", child],
                cwd=backend, env=environment, stderr=stderr,
            )
            deadline = time.time() + 120
            while cluster.status().status.value != "ready" and time.time() < deadline:
                time.sleep(0.1)
            time.sleep(4)
            result["status_before"] = cluster.status().status.value
            result["sent_at"] = time.time()
            ctrl_c()
            result["run_exit"] = run.wait(timeout=120)
        result["run_stderr"] = errors.read_text(errors="replace")
        saw = float(marker.read_text()) if marker.exists() else None
        result["child_saw_it_at"] = saw
        result["status"] = cluster.status().status.value
except Exception as error:
    result["error"] = repr(error)
finally:
    try:
        cluster.stop()
    except Exception:
        pass
Path(result_file).write_text(json.dumps(result), encoding="utf-8")
"""

    def _in_a_hidden_console(self, mode: str, home: Path) -> Any:
        result_file = home.parent / f"ctrl-c-{mode}.json"
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE: a console of its own, not on screen
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self.CTRL_C_SCRIPT,
                mode,
                str(self.runtime.root),
                str(home),
                str(result_file),
                str(BACKEND),
            ],
            cwd=BACKEND,
            env={**os.environ, "PYTHONPATH": str(BACKEND)},
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
        process.wait(timeout=400)
        if not result_file.exists():
            raise CheckFailedError(
                f"the helper for {mode!r} left no result (exit {process.returncode})"
            )
        return json.loads(result_file.read_text(encoding="utf-8"))

    def ctrl_c(self) -> str:
        adapter_home = self.copy_of_template("ctrl-c-adapter")
        adapter = self._in_a_hidden_console("adapter", adapter_home)
        say(
            f"server started by the adapter, then Ctrl+C sent to the caller's console: "
            f"{adapter}"
        )
        if adapter.get("status") != "ready" or not adapter.get("postmaster_alive"):
            raise CheckFailedError("the server fell to the caller's Ctrl+C")

        shared_home = self.copy_of_template("ctrl-c-shared")
        shared = self._in_a_hidden_console("shared", shared_home)
        say(
            f"control, pg_ctl without CREATE_NO_WINDOW, so the server shares the "
            f"console: {shared}"
        )

        run_home = self.copy_of_template("ctrl-c-run")
        # The copied log already holds the template's own shutdowns.
        began = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        run = self._in_a_hidden_console("run", run_home)
        log = server_log(ClusterHome(run_home).files(MAJOR))
        shutdowns = [
            line
            for line in log.splitlines()
            if "received fast shutdown request" in line and line[:19] >= began
        ]
        stopped = "stopped (fast)" in str(run.get("run_stderr"))
        say("`run -- <a command that sleeps>`, then Ctrl+C on its console:")
        say(
            f"  status before: {run.get('status_before')}; run exited "
            f"{run.get('run_exit')} (the command exits 42 on Ctrl+C)"
        )
        say(f"  the command saw the Ctrl+C: {run.get('child_saw_it_at') is not None}")
        say(
            f"  run said it stopped the cluster (fast): {stopped}; status after: "
            f"{run.get('status')}"
        )
        say(
            f"  'received fast shutdown request' in the server's log: {len(shutdowns)} "
            f"time(s)"
        )
        for line in shutdowns:
            say(f"    {line[:110]}")
        if run.get("child_saw_it_at") and shutdowns:
            stamp = SERVER_STAMP.match(shutdowns[-1])
            assert stamp is not None
            asked = datetime.datetime.strptime(
                stamp.group(1), "%Y-%m-%d %H:%M:%S.%f"
            ).timestamp()
            say(
                f"  the shutdown request came "
                f"{asked - float(run['child_saw_it_at']):+.3f} s after the command saw "
                f"Ctrl+C"
            )
        if (
            run.get("run_exit") != 42
            or not stopped
            or len(shutdowns) != 1
            or run.get("status") != "stopped"
        ):
            raise CheckFailedError(f"run did not behave as section 11 says: {run}")
        proof = (
            "the control, sharing the console, fell"
            if shared.get("status") != "ready"
            else "the control did not fall either"
        )
        return (
            f"the server did not fall; run stopped what it started, exit code kept; "
            f"{proof}"
        )

    # -- 25. paths -----------------------------------------------------------

    def paths(self) -> str:
        acp = int(kernel32.GetACP())
        say(f"ANSI code page: {acp}")
        cases: list[tuple[str, Path, PostgresRuntime]] = [
            (
                "user directory, space and accent inside the code page",
                self.work / "João Silva" / "SolidVision" / "postgres",
                self.runtime,
            ),
            (
                "user directory, characters outside the code page",
                self.work / "Łukasz 東京" / "SolidVision" / "postgres",
                self.runtime,
            ),
            (
                "runtime in a directory with a space",
                self.home("runtime-with-space"),
                self.runtime_copy(),
            ),
        ]
        outside = self.work / "Łukasz 東京 runtime" / "postgres"
        shutil.copytree(self.runtime.root, outside)
        cases.append(
            (
                "runtime in a directory outside the code page",
                self.home("runtime-outside-acp"),
                PostgresRuntime(outside),
            )
        )
        outcomes = []
        for label, home, runtime in cases:
            say(f"{label}:")
            say(f"  cluster: {home}")
            say(f"  runtime: {runtime.root}")
            for which, path in (("cluster", home), ("runtime", runtime.root)):
                try:
                    str(path).encode(f"cp{acp}")
                    fits = True
                except UnicodeEncodeError:
                    fits = False
                if not fits:
                    path.mkdir(parents=True, exist_ok=True)
                    short = short_path(path)
                    say(
                        f"  the {which} path does not fit cp{acp}; its 8.3 short form: "
                        f"{short or 'none on this volume'} (ASCII: "
                        f"{short.isascii() if short else '-'})"
                    )
            cluster = self.cluster(home, runtime=runtime)
            try:
                began = time.perf_counter()
                started = cluster.start()
                cluster.prepare(started.connection)
                first = time.perf_counter() - began
                pid_file = read_postmaster_pid(
                    ClusterHome(home).files(MAJOR).postmaster_pid
                )
                with psycopg.connect(started.connection.conninfo()) as db:
                    # As bytes: the server keeps this path in the ANSI code
                    # page, inside a UTF8 database, and psycopg cannot decode
                    # the text it would send.
                    row = db.execute(
                        "SELECT current_setting('data_directory')::bytea"
                    ).fetchone()
                cluster.stop()
                warm = cluster.start()
                adopted = cluster.start()
                cluster.stop()
                assert row is not None and pid_file is not None
                say(
                    f"  first run {seconds(first)}, then a warm start "
                    f"(started_here={warm.started_here}) and an adoption "
                    f"(started_here={adopted.started_here}): OK"
                )
                raw = bytes(row[0])
                try:
                    raw.decode("utf-8")
                    as_text = "valid UTF-8"
                except UnicodeDecodeError:
                    as_text = "NOT valid UTF-8: a client asking for it as text fails"
                say(
                    f"  the server's own data_directory: "
                    f"{raw.decode('mbcs', 'replace')} ({as_text})"
                )
                say(
                    f"  postmaster.pid line 2 points at this data\\: "
                    f"{pid_file.points_to(ClusterHome(home).files(MAJOR).data)}"
                )
                outcomes.append((label, "works"))
            except Exception as error:  # the result of this check *is* the error
                self.stop_quietly(cluster)
                text = str(error).strip()
                say(f"  FAILS: {type(error).__name__}: {text[:600]}")
                outcomes.append((label, f"fails ({type(error).__name__})"))
        return "; ".join(f"{label}: {result}" for label, result in outcomes)

    # -- 26. DLLs ------------------------------------------------------------

    def dlls(self) -> str:
        root = self.runtime.root
        binaries = [
            self.runtime.postgres,
            self.runtime.initdb,
            self.runtime.pg_ctl,
            root / "Library" / "lib" / "vector.dll",
        ]
        system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
        known = known_dlls()
        search = [self.runtime.bin_directory, system32, system32.parent, root]

        def resolve(name: str) -> tuple[str, Path | None]:
            lower = name.lower()
            if lower.startswith(("api-ms-win-", "ext-ms-")):
                return "API set (Windows)", None
            if lower in known:
                return "KnownDLL (Windows)", system32 / name
            for directory in search:
                candidate = directory / name
                if candidate.is_file():
                    if self.runtime.contains(candidate):
                        return "runtime", candidate
                    if windows_protects(candidate):
                        return "Windows", candidate
                    product = product_name(candidate) or "no product name"
                    return f"NOT Windows, NOT runtime ({product})", candidate
            return "UNRESOLVED", None

        total_problems = []
        for binary in binaries:
            pending, seen = [binary], {binary.name.lower()}
            classes: dict[str, set[str]] = {}
            while pending:
                current = pending.pop()
                for name in pe_imports(current):
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    kind, location = resolve(name)
                    classes.setdefault(kind, set()).add(name)
                    if kind == "runtime" and location is not None:
                        pending.append(location)
            summary = ", ".join(
                f"{kind}: {len(names)}" for kind, names in sorted(classes.items())
            )
            say(
                f"{binary.name}: {len(pe_imports(binary))} direct imports; closure -> "
                f"{summary}"
            )
            say(
                f"  from the runtime: "
                f"{', '.join(sorted(classes.get('runtime', set())))}"
            )
            problems = {
                k: v for k, v in classes.items() if k.startswith(("NOT", "UNRESOLVED"))
            }
            for kind, names in problems.items():
                say(f"  {kind}: {', '.join(sorted(names))}")
                total_problems += sorted(names)

        say("the root of the runtime, by the package that put each file there:")
        owners: dict[str, str] = {}
        for meta in (root / "conda-meta").glob("*.json"):
            record = json.loads(meta.read_text(encoding="utf-8"))
            for relative in record.get("files", []):
                if "/" not in relative and "\\" not in relative:
                    owners[relative.lower()] = record["name"]
        by_package: dict[str, list[int]] = {}
        for path in root.iterdir():
            if path.is_file():
                by_package.setdefault(
                    owners.get(path.name.lower(), "(no package)"), []
                ).append(path.stat().st_size)
        for package, sizes in sorted(
            by_package.items(), key=lambda item: -sum(item[1])
        ):
            say(f"  {package:<22} {len(sizes):>3} files, {mb(sum(sizes))}")
        loose = sum(sum(sizes) for sizes in by_package.values())
        say(f"  total {mb(loose)} in loose files (RFC-033a step 3 measured 5.0 MB)")

        # What a running server has actually loaded, and from where.
        home = self.copy_of_template("dlls")
        cluster = self.cluster(home)
        started = cluster.start()
        try:
            with psycopg.connect(started.connection.conninfo()) as db:
                db.execute("SELECT '[1,2,3]'::vector <=> '[3,2,1]'::vector")
                backend = db.execute("SELECT pg_backend_pid()").fetchone()
                assert backend is not None
                loaded = {}
                for label, pid in (
                    ("postmaster", self.postmaster(home)),
                    ("a backend that used vector", int(backend[0])),
                ):
                    loaded[label] = loaded_modules(pid)
        finally:
            cluster.stop()
        windows = system32.parent
        elsewhere_total = []
        for label, modules in loaded.items():
            inside = [m for m in modules if self.runtime.contains(m)]
            from_windows = [
                m for m in modules if str(m).lower().startswith(str(windows).lower())
            ]
            elsewhere = [
                m for m in modules if m not in inside and m not in from_windows
            ]
            elsewhere_total += elsewhere
            say(
                f"{label}: {len(modules)} modules loaded -- {len(inside)} from the "
                f"runtime, {len(from_windows)} from {windows}, {len(elsewhere)} from "
                f"anywhere else"
            )
            for module in elsewhere:
                say(f"  ELSEWHERE: {module}")
            places: dict[str, int] = {}
            for module in inside:
                place = os.path.relpath(
                    os.path.realpath(module.parent), os.path.realpath(root)
                )
                places[place] = places.get(place, 0) + 1
            say(
                "  from the runtime, by directory: "
                + ", ".join(
                    f"{place} {count}" for place, count in sorted(places.items())
                )
            )
            for wanted in (
                "vcruntime140.dll",
                "ucrtbase.dll",
                "libxml2.dll",
                "libssl-3-x64.dll",
                "vector.dll",
            ):
                found = [m for m in modules if m.name.lower() == wanted]
                say(f"  {wanted:<18} {found[0] if found else 'not loaded'}")
            icu = sorted({m.name for m in modules if m.name.lower().startswith("icu")})
            say(f"  ICU DLLs loaded: {', '.join(icu) or 'none'}")
        say("what System32 has under the Visual C++ runtime's names, on this machine:")
        for name in ("vcruntime140.dll", "msvcp140.dll", "ucrtbase.dll"):
            there = system32 / name
            if there.is_file():
                say(
                    f"  {name:<18} present; part of Windows itself: "
                    f"{windows_protects(there)}"
                )
            else:
                say(f"  {name:<18} absent")
        if total_problems or elsewhere_total:
            raise CheckFailedError(
                f"not closed: {total_problems + [str(m) for m in elsewhere_total]}"
            )
        return (
            "closed: every import of the four binaries resolves inside the runtime or "
            "to "
            f"Windows itself; a live server loaded nothing from anywhere else; "
            f"{mb(loose)} loose in the root"
        )

    # -- 27. the user's environment ------------------------------------------

    def user_environment(self) -> str:
        decoy = self.work / "decoy-on-PATH"
        decoy.mkdir()
        for name in (
            "libpq.dll",
            "libssl-3-x64.dll",
            "libcrypto-3-x64.dll",
            "zlib.dll",
            "vcruntime140.dll",
        ):
            (decoy / name).write_bytes(b"this is not a DLL")
        home = self.copy_of_template("user-environment")
        environment = dict(os.environ)
        environment.update(
            PGPORT="1",
            PGDATA=str(self.work / "not-a-cluster"),
            PGOPTIONS="-c default_transaction_read_only=on",
            PGPASSWORD="wrong",
            PGHOST="example.invalid",
            PATH=str(decoy) + os.pathsep + environment.get("PATH", ""),
        )
        files = ClusterHome(home).files(MAJOR)
        # A database prepare() has to create: a read-only PGOPTIONS would stop it.
        process = self.child(home, "start,prepare", environment=environment)
        outcome = self.child_result(process)
        say(
            f"PGPORT=1, PGDATA=<nothing>, PGOPTIONS=read-only, PGPASSWORD=wrong, "
            f"PGHOST=invalid, and {decoy.name}\\ first on PATH with five fake DLLs:"
        )
        say(f"  start + prepare in that environment: {outcome}")
        if "error" in outcome:
            raise CheckFailedError(outcome["error"])
        postmaster = self.postmaster(home)
        from_decoy = [
            m
            for m in loaded_modules(postmaster)
            if str(m).lower().startswith(str(decoy).lower())
        ]
        pid_file = read_postmaster_pid(files.postmaster_pid)
        assert pid_file is not None
        say(
            f"  the server runs from this data\\: {pid_file.points_to(files.data)}; on "
            f"port {pid_file.port} (not 1)"
        )
        say(
            f"  modules the postmaster loaded from the decoy directory: "
            f"{len(from_decoy)}"
        )
        connection = ClusterConnection(
            port=int(outcome["port"]), password=files.password.read_text().strip()
        )
        with psycopg.connect(connection.conninfo("postgres")) as db:
            created = db.execute(
                "SELECT datname FROM pg_database WHERE datname = 'rfc033_env_probe'"
            ).fetchone()
        self.cluster(home).stop()
        del created
        if from_decoy or pid_file.port == 1:
            raise CheckFailedError("the user's environment reached the cluster")
        return (
            "nothing changed: same cluster, its own port, no DLL from the PATH, "
            "database prepared"
        )

    # -- 28. DACL ------------------------------------------------------------

    def dacl(self) -> str:
        home = self.template()
        files = ClusterHome(home).files(MAJOR)
        sid = current_user_sid()
        root = dacl_sddl(home)
        password = dacl_sddl(files.password)
        data = dacl_sddl(files.data)
        say(f"current user: {sid}")
        say(f"postgres\\         {root}")
        say(f"16\\password      {password}")
        say(f"16\\data          {data}")
        entries = set(re.findall(r"\(([^)]*)\)", root))
        expected = {"A;OICI;FA;;;SY", f"A;OICI;FA;;;{sid}"}
        who = {
            entry.rsplit(";", 1)[1]
            for sddl in (password, data)
            for entry in re.findall(r"\(([^)]*)\)", sddl)
        }
        if not root.startswith("D:P") or entries != expected or who != {"SY", sid}:
            raise CheckFailedError("the DACL is not exactly the user and S-1-5-18")
        return (
            "protected (inheritance cut), full control for the user's SID and S-1-5-18 "
            "(SY) only; inherited by password and data"
        )

    # -- 31, 32. refusals ----------------------------------------------------

    def newer_database(self) -> str:
        home = self.copy_of_template("newer-database")
        cluster = self.cluster(home)
        started = cluster.start()
        try:
            with psycopg.connect(started.connection.conninfo(), autocommit=True) as db:
                db.execute("UPDATE alembic_version SET version_num = '0123456789ab'")
            # The counters are flushed when a session ends and read a moment
            # later: this UPDATE must be in `before`, not charged to prepare().
            time.sleep(1.5)
            with psycopg.connect(started.connection.conninfo(), autocommit=True) as db:
                before = db.execute(
                    "SELECT xact_commit, tup_inserted, tup_updated, tup_deleted FROM "
                    "pg_stat_database WHERE datname = current_database()"
                ).fetchone()
            try:
                cluster.prepare(started.connection)
                raise CheckFailedError("a database newer than the code was accepted")
            except ClusterRefusedError as refusal:
                message = str(refusal)
            time.sleep(1.5)
            with psycopg.connect(started.connection.conninfo(), autocommit=True) as db:
                version = db.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
                after = db.execute(
                    "SELECT xact_commit, tup_inserted, tup_updated, tup_deleted FROM "
                    "pg_stat_database WHERE datname = current_database()"
                ).fetchone()
        finally:
            cluster.stop()
        assert before is not None and after is not None
        say(
            "alembic_version set to a revision the code does not know; prepare() "
            "refused:"
        )
        say(f"  {message}")
        say(f"  alembic_version afterwards: {version}")
        say(
            f"  rows inserted/updated/deleted in the database by the refusal: "
            f"{after[1] - before[1]}/{after[2] - before[2]}/{after[3] - before[3]}"
        )
        if version != [("0123456789ab",)] or after[1:] != before[1:]:
            raise CheckFailedError("the refusal wrote to the database")
        return "refused with the message, nothing written"

    def other_major(self) -> str:
        home = self.copy_of_template("other-major")
        files = ClusterHome(home).files(MAJOR)
        files.pg_version.write_text("15\n", encoding="ascii")
        before = {
            p.relative_to(files.data): p.stat().st_mtime_ns
            for p in files.data.rglob("*")
        }
        cluster = self.cluster(home)
        try:
            cluster.start()
            raise CheckFailedError("a cluster of another major version was started")
        except ClusterRefusedError as refusal:
            message = str(refusal)
        after = {
            p.relative_to(files.data): p.stat().st_mtime_ns
            for p in files.data.rglob("*")
        }
        say("PG_VERSION forged as 15; start() refused:")
        say(f"  {message}")
        say(
            f"  data\\ untouched ({len(before)} entries, same names and timestamps): "
            f"{before == after}; data.initdb created: {files.data_initdb.exists()}"
        )
        say(f"  status(): {cluster.status().status.value}")
        if before != after or files.data_initdb.exists():
            raise CheckFailedError("the refused cluster was touched")
        return "refused; data\\ untouched, no initdb over it"

    # -- 30. firewall --------------------------------------------------------

    def firewall(self) -> str:
        state = subprocess.run(
            ["netsh", "advfirewall", "show", "allprofiles", "state"],
            capture_output=True,
            text=True,
            encoding="oem",
            errors="replace",
        ).stdout
        on = re.findall(r"^(?:Estado|State)\s+(\S+)", state, re.MULTILINE)

        # The first start of an executable the firewall has never been asked
        # about: a copy of the runtime under a path of its own, started with
        # the desktop's windows being watched.
        fresh = PostgresRuntime(self.work / "never seen by the firewall" / "postgres")
        shutil.copytree(self.runtime.root, fresh.root)
        cluster = self.cluster(self.copy_of_template("firewall"), runtime=fresh)
        before = visible_windows()
        seen: dict[int, tuple[str, str, int]] = {}
        done = threading.Event()

        def watch() -> None:
            while not done.is_set():
                seen.update(visible_windows())
                time.sleep(0.02)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            started = cluster.start()
            with psycopg.connect(started.connection.conninfo("postgres")) as db:
                row = db.execute(
                    "SELECT current_setting('listen_addresses'), "
                    "host(inet_server_addr())"
                ).fetchone()
            time.sleep(3.0)
        finally:
            done.set()
            watcher.join(timeout=5)
            self.stop_quietly(cluster)
        assert row is not None
        appeared = {h: info for h, info in seen.items() if h not in before}
        alerts = [
            line
            for line in describe_windows(appeared)
            if "#32770" in line
            or "firewall" in line.lower()
            or "Security" in line
            or "Seguran" in line
        ]
        say(
            f"first start of a postgres.exe at a path never run before "
            f"({fresh.root.parent.name}\\), listening on {row[0]} ({row[1]}):"
        )
        say(
            f"  windows that appeared on the desktop in the 3 s after it: "
            f"{len(appeared)}, of which dialogs or security alerts: {len(alerts)}"
        )
        for line in describe_windows(appeared):
            say(f"    {line}")
        self.window_notes += alerts
        rules = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "verbose"],
            capture_output=True,
            text=True,
            encoding="oem",
            errors="replace",
        ).stdout
        mentions = [
            line.strip()
            for line in rules.splitlines()
            if str(self.runtime.root).lower() in line.lower()
            or str(self.work).lower() in line.lower()
        ]
        any_postgres = [
            line.strip()
            for line in rules.splitlines()
            if "postgres.exe" in line.lower()
        ]
        dialogs = [
            note
            for note in self.window_notes
            if "#32770" in note or "Security" in note or "Seguran" in note
        ]
        say(
            f"Defender Firewall, the three profiles: "
            f"{', '.join(on) or 'state not read'}"
        )
        say(
            f"  firewall rules naming this runtime or this run's copies of it: "
            f"{len(mentions)}"
        )
        say(f"  firewall rules naming any postgres.exe at all: {len(any_postgres)}")
        say(
            f"  (the rule listing, read without elevation, is "
            f"{len(rules.splitlines())} lines long)"
        )
        say(
            f"  dialogs among the windows seen in this check and in `no-console`: "
            f"{len(dialogs)}"
        )
        if mentions or dialogs:
            raise CheckFailedError(
                "a firewall alert or rule appeared for the embedded server"
            )
        return (
            f"firewall {'/'.join(on)}; a postgres.exe never run before started "
            f"with no dialog and left no rule (it listens on {row[0]} only)"
        )

    # -- 29. the password does not leak --------------------------------------

    def password_leak(self) -> str:
        home = self.copy_of_template("password-leak")
        environment = {
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "EMBEDDED_POSTGRES_DIRECTORY": str(home),
        }
        echo = [
            sys.executable,
            "-c",
            "import os; print(sorted(k for k in os.environ if "
            "k.startswith('DATABASE_')))",
        ]
        for arguments in (
            ["start"],
            ["status"],
            ["run", "--", *echo],
            ["stop"],
            ["status"],
        ):
            done = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.infrastructure.database.embedded",
                    *arguments,
                ],
                cwd=BACKEND,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.cli_output.append(done.stdout + done.stderr)
        passwords = {
            path.read_text(encoding="ascii").strip()
            for path in self.work.rglob("password")
            if path.is_file()
        }
        passwords.discard("")
        haystacks: list[tuple[str, bytes]] = []
        for path in (REPO / "logs").glob("*"):
            if path.is_file():
                haystacks.append((f"app log {path.name}", path.read_bytes()))
        counted = {
            "app log": len(haystacks),
            "server log": 0,
            "startup.log / pg_ctl.log": 0,
            "conf": 0,
        }
        for password_file in self.work.rglob("password"):
            directory = password_file.parent
            for name in ("startup.log", "pg_ctl.log"):
                if (directory / name).is_file():
                    haystacks.append(
                        (str(directory / name), (directory / name).read_bytes())
                    )
                    counted["startup.log / pg_ctl.log"] += 1
            for name in ("solidvision.conf", "postgresql.conf"):
                if (directory / "data" / name).is_file():
                    haystacks.append(
                        (
                            str(directory / "data" / name),
                            (directory / "data" / name).read_bytes(),
                        )
                    )
                    counted["conf"] += 1
            for path in (directory / "data" / "log").glob("*"):
                haystacks.append((str(path), path.read_bytes()))
                counted["server log"] += 1
        haystacks.append(
            (
                "the CLI's output (start, status, run, stop)",
                "".join(self.cli_output).encode(),
            )
        )
        haystacks.append(("this script's own output", "".join(self.tee.text).encode()))
        hits = [
            name
            for name, content in haystacks
            for password in passwords
            if password.encode("ascii") in content
        ]
        say(
            f"{len(passwords)} passwords were generated by this run; each was searched "
            f"for in:"
        )
        for kind, count in counted.items():
            say(f"  {count:>3} x {kind}")
        say(
            f"    1 x the CLI's captured output, {len(''.join(self.cli_output))} "
            f"characters"
        )
        say("    1 x this script's own output so far")
        say(f"occurrences: {len(hits)}")
        for name in hits[:10]:
            say(f"  FOUND in {name}")
        if hits:
            raise CheckFailedError(f"a password was found in {len(hits)} place(s)")
        return (
            f"{len(passwords)} passwords, {len(haystacks)} files and outputs searched: "
            f"no occurrence"
        )

    # -- 33. logoff ----------------------------------------------------------

    def logoff(self) -> str:
        say("MANUAL, and not automatable from inside the session it ends:")
        say(
            "  1. python -m app.infrastructure.database.embedded start   (from "
            "backend\\)"
        )
        say("  2. log off Windows, log on again")
        say(
            "  3. python -m app.infrastructure.database.embedded status  -> expected: "
            "stopped"
        )
        say(
            "  4. python -m app.infrastructure.database.embedded start   -> expected: "
            "'recovered from an unclean shutdown'"
        )
        return "MANUAL: not run"

    # -- 34. style and types -------------------------------------------------

    def style(self) -> str:
        targets = [
            "backend/app/infrastructure/database/embedded",
            "backend/tests/infrastructure/database/embedded",
            "backend/app/infrastructure/config/settings.py",
            "scripts/vendor_postgres.py",
            "experiments/rfc-033-embedded-postgres/measure_adapter.py",
        ]
        results = []
        black = [
            self.black_python or sys.executable,
            "-m",
            "black",
            "--check",
            *targets,
        ]
        tools = {
            "black --check": black,
            "ruff check": [sys.executable, "-m", "ruff", "check", "--no-fix", *targets],
            "mypy": [sys.executable, "-m", "mypy", *targets[:3]],
        }
        failed = []
        for name, command in tools.items():
            done = subprocess.run(
                command,
                cwd=REPO,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            last = (done.stdout + done.stderr).strip().splitlines()[-1:]
            say(f"{name}: exit {done.returncode} -- {last[0] if last else ''}")
            results.append(f"{name} {'clean' if done.returncode == 0 else 'FAILED'}")
            if done.returncode != 0:
                failed.append(name)
        if self.black_python:
            say(
                f"  (black run with {self.black_python}: black refuses to run on "
                f"Python 3.12.5)"
            )
        if failed:
            raise CheckFailedError(f"{', '.join(failed)} not clean")
        return ", ".join(results)

    # -- 9, 10, 35. suites (opt-in) ------------------------------------------

    PROBE = (
        "import json\n"
        "from sqlalchemy import text\n"
        "from app.infrastructure.config.settings import settings\n"
        "from app.infrastructure.persistence.engine import EngineInstance\n"
        "with EngineInstance.connect() as connection:\n"
        "    port, version = connection.execute(\n"
        "        text('SELECT inet_server_port(), version()')).one()\n"
        "print('RFC033-PROBE ' + json.dumps({'settings': settings.database_port,\n"
        "    'host': settings.database_host, 'port': port, 'version': version}))\n"
    )

    @staticmethod
    def _summary(output: str) -> str:
        for line in reversed(output.splitlines()):
            if re.search(r"\d+ (passed|failed|error)", line):
                return line.strip("= ").strip()
        return "no summary line"

    def suite(self) -> str:
        home = self.copy_of_template("suite")
        cluster = self.cluster(home)
        environment = {
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "EMBEDDED_POSTGRES_DIRECTORY": str(home),
            "PYTHONIOENCODING": "utf-8",
        }
        cli = [
            sys.executable,
            "-m",
            "app.infrastructure.database.embedded",
            "run",
            "--",
        ]
        docker = docker_status()
        say(f"docker: {docker}")
        probe = subprocess.run(
            [*cli, sys.executable, "-c", self.PROBE],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        found = [
            line.split(" ", 1)[1]
            for line in probe.stdout.splitlines()
            if line.startswith("RFC033-PROBE ")
        ]
        if not found:
            raise CheckFailedError(
                f"the probe did not answer: {probe.stderr.strip()[-300:]}"
            )
        target = json.loads(found[0])
        say(
            f"`run -- <probe>`: the project's settings say "
            f"{target['host']}:{target['settings']}; the server it reached says port "
            f"{target['port']}, {target['version']}"
        )
        say(
            f"  and after it, the cluster is: {cluster.status().status.value} (run "
            f"stopped what it started)"
        )
        if (
            target["settings"] != target["port"]
            or "Visual C++" not in target["version"]
            or target["port"] == 5432
        ):
            raise CheckFailedError(
                "the project's settings do not reach the adapter's cluster"
            )

        def counters() -> tuple[int, int]:
            started = cluster.start()
            with psycopg.connect(started.connection.conninfo(), autocommit=True) as db:
                row = db.execute(
                    "SELECT xact_commit, xact_rollback FROM pg_stat_database "
                    "WHERE datname = current_database()"
                ).fetchone()
            cluster.stop()
            assert row is not None
            return int(row[0]), int(row[1])

        # What the .env's database does with nobody using it: autovacuum and
        # the container's health check commit there on their own.
        idle = None
        resting = self._env_database_counters()
        if resting is not None:
            time.sleep(30)
            rested = self._env_database_counters()
            if rested is not None:
                idle = (rested[0] - resting[0], rested[1] - resting[1])
        before = counters()
        other_before = self._env_database_counters()
        began = time.perf_counter()
        done = subprocess.run(
            [*cli, sys.executable, "-m", "pytest"],
            cwd=REPO,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - began
        after = counters()
        other_after = self._env_database_counters()
        summary = self._summary(done.stdout)
        commits, rollbacks = after[0] - before[0], after[1] - before[1]
        say(f"`run -- python -m pytest`: exit {done.returncode} in {seconds(elapsed)}")
        say(f"  {summary}")
        for line in done.stdout.splitlines():
            if line.startswith(("FAILED ", "ERROR ")):
                say(f"  {line}")
        say(
            f"  on the adapter's cluster: xact_commit {before[0]} -> {after[0]} "
            f"(+{commits}), xact_rollback {before[1]} -> {after[1]} (+{rollbacks})"
        )
        say(f"  the cluster afterwards: {cluster.status().status.value}")
        if other_before is None or other_after is None:
            other = "unreachable"
            say("  the .env's database was not reachable: nowhere else to go")
        else:
            other_commits = other_after[0] - other_before[0]
            other_rollbacks = other_after[1] - other_before[1]
            other = f"+{other_commits} commits, +{other_rollbacks} rollbacks"
            say(
                f"  on the .env's database, the one a misnamed variable would fall "
                f"back to: xact_commit +{other_commits}, xact_rollback "
                f"+{other_rollbacks} in those {seconds(elapsed)}"
            )
            if idle is not None:
                say(
                    f"    left alone for 30 s before the suite, the same database "
                    f"moved by +{idle[0]} commits and +{idle[1]} rollbacks"
                )
        if commits <= 10:
            raise CheckFailedError("the suite did not talk to the adapter's cluster")
        # The suite rolls back every database test: rollbacks are its
        # signature, and nothing that runs by itself in the other database
        # produces one.
        if other_before is not None and other_after is not None:
            if other_after[1] - other_before[1] > 5:
                raise CheckFailedError("the suite also talked to the .env's database")
        return (
            f"{summary}; port {target['port']} through the project's settings; "
            f"+{commits} commits and +{rollbacks} rollbacks on the adapter's "
            f"cluster, {other} on the .env's database; docker: {docker}"
        )

    @staticmethod
    def _env_database_counters() -> tuple[int, int] | None:
        """`xact_commit` and `xact_rollback` of the database the `.env` points at.

        The other side of the proof of section 4.2 of RFC-033a, for a run
        with Docker up: the adapter's cluster must have been used, and this
        one must not.
        """
        from app.infrastructure.config.settings import Settings

        settings = Settings()
        try:
            with psycopg.connect(
                host=settings.database_host,
                port=settings.database_port,
                user=settings.database_user,
                password=settings.database_password,
                dbname=settings.database_name,
                connect_timeout=5,
                autocommit=True,
            ) as db:
                row = db.execute(
                    "SELECT xact_commit, xact_rollback FROM pg_stat_database "
                    "WHERE datname = current_database()"
                ).fetchone()
        except psycopg.Error:
            return None
        return (int(row[0]), int(row[1])) if row else None

    def embedded_tests(self) -> str:
        began = time.perf_counter()
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "embedded",
                "-p",
                "no:cacheprovider",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        summary = self._summary(done.stdout)
        say(
            f"pytest -m embedded: exit {done.returncode} in "
            f"{seconds(time.perf_counter() - began)} -- {summary}"
        )
        if done.returncode != 0:
            raise CheckFailedError(summary)
        return summary

    def docker_suite(self) -> str:
        docker = docker_status()
        say(f"docker: {docker}")
        began = time.perf_counter()
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        summary = self._summary(done.stdout)
        say(
            f"pytest, plain, against the .env: exit {done.returncode} in "
            f"{seconds(time.perf_counter() - began)} -- {summary}"
        )
        for line in done.stdout.splitlines():
            if line.startswith(("FAILED ", "ERROR ")):
                say(f"  {line}")
        return f"{summary}; docker: {docker}"

    # -- the last check ------------------------------------------------------

    def nothing_left(self) -> str:
        ours = self.ours_running()
        since = [
            entry
            for entry in runtime_processes(self.runtime)
            if entry[0] not in self.preexisting and entry not in ours
        ]
        say(
            f"servers with a lock file under the work directory, their children, "
            f"and processes of the runtime's copies: {ours or 'none'}"
        )
        say(
            f"other processes of the runtime that appeared during the run: "
            f"{since or 'none'}"
        )
        say(
            f"processes of the runtime that were already there, left alone: "
            f"{len(self.preexisting)}"
        )
        if ours or since:
            raise CheckFailedError(f"left running: {ours + since}")
        return "no process of this run's clusters, nor of its runtime copies, left"


def same_machine_baselines(storage: str) -> list[str]:
    """What RFC-033a's script measured on the disk this run is on.

    The non-regression rule of section 18 compares with RFC-033a's numbers,
    and those are a hard disk's. Its script's logs beside this one say what
    it measures here; the ones from another disk are left out.
    """
    disk = storage.partition(" on ")[2].partition(" (")[0]
    found = []
    for log in sorted(HERE.glob("measure_embedded_postgres*.log")):
        text = log.read_text(encoding="utf-8", errors="replace")
        if not disk or disk not in text:
            continue

        def value(label: str, source: str = text) -> str:
            match = re.search(rf"^\s*{re.escape(label)}\s+([\d.]+ s)", source, re.M)
            return match.group(1) if match else "?"

        when = re.search(r"^when:\s+(\S+ \S+)", text, re.M)
        found.append(
            f"{when.group(1) if when else log.name}: initdb "
            f"{value('initdb (once, per machine):')}, listening -> ready "
            f"{value('listening -> ready:')}, alembic (subprocess) "
            f"{value('alembic upgrade head:')}"
        )
    return found


def docker_status() -> str:
    if shutil.which("docker") is None:
        return "not installed"
    try:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if info.returncode != 0 or not info.stdout.strip():
            return "installed, NOT running"
        containers = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} ({{.Image}})"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        running = (
            ", ".join(containers.stdout.split("\n")).strip(", ")
            or "no container running"
        )
        return f"daemon running (engine {info.stdout.strip()}); {running}"
    except (OSError, subprocess.TimeoutExpired):
        return "installed, daemon not answering"


def storage_of(path: Path) -> str:
    drive = path.resolve().drive.rstrip(":")
    command = (
        f"$d = Get-Partition -DriveLetter {drive} | Get-Disk; "
        "Get-PhysicalDisk | Where-Object DeviceId -eq $d.Number | "
        'ForEach-Object { "$($_.FriendlyName)|$($_.MediaType)|$($_.BusType)" }'
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"{drive}: medium unknown"
    found = done.stdout.strip().split("|")
    return (
        f"{drive}: on {found[0]} ({found[1]}, {found[2]})"
        if len(found) == 3
        else f"{drive}: medium unknown"
    )


CHECKS: list[tuple[str, str, str, bool]] = [
    # (id, section 18 row, method, runs by default)
    ("first-run", "First run", "first_run", True),
    ("initdb-fsync", "How much of initdb is fsync", "initdb_fsync", True),
    ("copy-data", "Copying a ready data\\, for the installer", "copy_data", True),
    ("warm-start", "Warm start, and stop", "warm_start", True),
    ("adoption", "Adoption", "adoption", True),
    ("noop-migrations", "Migrations with nothing to migrate", "noop_migrations", True),
    ("idle-memory", "Idle memory", "idle_memory", True),
    ("facts", "The facts of the cluster", "facts", True),
    ("killed-postmaster", "Postmaster killed by force", "killed_postmaster", True),
    ("killed-tree", "Tree killed", "killed_tree", True),
    ("killed-backend", "One backend killed", "killed_backend", True),
    ("immediate", "immediate", "immediate", True),
    ("orphans", "Orphans", "orphans", True),
    ("initdb-interrupted", "initdb interrupted", "initdb_interrupted", True),
    (
        "killed-in-recovery",
        "A start killed during recovery",
        "killed_in_recovery",
        True,
    ),
    (
        "pg-ctl-stale-lock",
        "What pg_ctl start -w says over a stale lock file",
        "pg_ctl_stale_lock",
        True,
    ),
    ("runtime-psql", "What the runtime's psql leaves behind", "runtime_psql", True),
    ("busy-port", "Busy port", "busy_port", True),
    ("reserved-range", "Reserved range", "reserved_range", True),
    ("two-starts", "Two simultaneous start()", "two_starts", True),
    ("elevated", "Elevated process", "elevated", True),
    ("no-console", "No console", "no_console", True),
    ("ctrl-c", "Ctrl+C", "ctrl_c", True),
    ("paths", "Paths", "paths", True),
    ("dlls", "DLLs", "dlls", True),
    ("user-environment", "The user's environment", "user_environment", True),
    ("dacl", "DACL", "dacl", True),
    ("newer-database", "A database newer than the code", "newer_database", True),
    ("other-major", "Another major version", "other_major", True),
    ("firewall", "Firewall", "firewall", True),
    ("logoff", "Logoff", "logoff", True),
    ("style", "Style and types", "style", True),
    ("embedded-tests", "pytest -m embedded", "embedded_tests", False),
    ("suite", "The suite against the adapter's cluster", "suite", False),
    ("docker-suite", "Docker still works", "docker_suite", False),
    ("password-leak", "The password does not leak", "password_leak", True),
    ("nothing-left", "Nothing left running", "nothing_left", True),
]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip", default="")
    parser.add_argument("--with", dest="extra", default="")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--black-python", help="an interpreter that can run black")
    parser.add_argument(
        "--kills",
        type=int,
        default=3,
        help="how many times each kill-under-load check kills (default 3)",
    )
    args = parser.parse_args()
    if args.list:
        for name, title, _, default in CHECKS:
            print(f"{name:<20} {title}{'' if default else '   (opt-in: --with)'}")
        return 0
    only = {name for name in args.only.split(",") if name}
    skip = {name for name in args.skip.split(",") if name}
    extra = {name for name in args.extra.split(",") if name}
    unknown = (only | skip | extra) - {name for name, *_ in CHECKS}
    if unknown:
        print(f"unknown checks: {sorted(unknown)}")
        return 2

    work = (
        args.work.resolve() if args.work else Path(tempfile.mkdtemp(prefix="rfc033-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    work_note = open_to_the_user(work) if is_elevated() else None
    measurement = Measurement(work, args.black_python, args.kills)
    sys.stdout = measurement.tee  # type: ignore[assignment]
    logging.getLogger("app").addHandler(ADAPTER_LOG)
    time_prepare_steps()
    manifest = measurement.runtime.manifest()

    print("=" * 78)
    print("RFC-033 section 18: the embedded PostgreSQL adapter, measured")
    print("=" * 78)
    print(f"when:     {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}")
    print(f"work dir: {work}")
    print(f"repo:     {REPO}")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    print(f"commit:   {commit}{' + uncommitted changes' if dirty else ''}")
    print(
        f"runtime:  {RUNTIME_DIRECTORY} (postgresql {manifest.postgresql}, pgvector "
        f"{manifest.pgvector}, lockfile {manifest.lockfile_sha256[:12]})"
    )
    print()
    print("conditions")
    print(f"    platform:   {platform.platform()}, {platform.machine()}")
    print(f"    python:     {platform.python_version()}")
    print(f"    cpu:        {platform.processor()} x{os.cpu_count()}")
    print(f"    storage:    {storage_of(work)}")
    print(f"    docker:     {docker_status()}")
    print(
        f"    ANSI code page: {kernel32.GetACP()}; this process elevated: "
        f"{is_elevated()}"
    )
    defender = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-MpComputerStatus).RealTimeProtectionEnabled",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"    Defender real-time protection: {defender or 'not read'}")
    if work_note:
        print(f"    work dir, made by an elevated process: {work_note}")
    print(
        f"    processes of the runtime already running, not this run's: "
        f"{len(measurement.preexisting)}"
    )
    overrides = {
        name: value
        for name, value in os.environ.items()
        if name in ("WARM_UP_MODELS", "ENVIRONMENT") or name.startswith("DATABASE_")
    }
    print(f"    settings overridden in the environment: {overrides or 'none'}")

    failed = []
    try:
        for name, title, method, default in CHECKS:
            chosen = name in only if only else (default or name in extra)
            if not chosen or name in skip:
                continue
            print()
            print(f"--- {title}  [{name}]")
            sys.stdout.flush()
            began = time.perf_counter()
            try:
                result = getattr(measurement, method)()
                measurement.verdict.setdefault(name, result)
                measurement.verdict[name] = result
            except CheckFailedError as error:
                measurement.verdict[name] = f"FAILED: {error}"
                failed.append(name)
                say(f"FAILED: {error}")
            except Exception as error:  # the next check still has to run
                measurement.verdict[name] = (
                    f"ERROR: {type(error).__name__}: {str(error)[:200]}"
                )
                failed.append(name)
                say(f"ERROR: {type(error).__name__}: {error}")
                for line in traceback.format_exc().splitlines()[-8:]:
                    say(f"  {line}")
            say(f"[{time.perf_counter() - began:.1f} s]")
    finally:
        # Only what this run is answerable for. A cluster of the same runtime
        # that someone else started -- a real one, in `%LOCALAPPDATA%` -- is
        # not this script's to end.
        for process_id, _ in measurement.ours_running():
            taskkill(process_id, tree=True)

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    for name, title, _, _ in CHECKS:
        if name in measurement.verdict:
            print(f"  {title}")
            print(f"      {measurement.verdict[name]}")
    if "stop" in measurement.verdict:
        print("  Stop")
        print(f"      {measurement.verdict['stop']}")
    print()
    if args.keep:
        print(f"  kept: {work}")
    else:
        time.sleep(1.0)
        shutil.rmtree(work, ignore_errors=True)
        print(f"  {'NOT fully removed' if work.exists() else 'removed'}: {work}")
    print(f"  checks failed: {', '.join(failed) or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
