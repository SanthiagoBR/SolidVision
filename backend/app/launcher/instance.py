r"""One launcher per data root: the lock, the state file, the stop event (RFC-036 8).

    <data>\launcher.lock    held exclusively for the launcher's whole life
    <data>\launcher.json    what it is doing, rewritten in one rename
    Local\SolidVision-launcher-<16 hex of the sha256 of the data root>
                            the named event `python -m app.launcher stop` signals

**The lock is the truth about there being a launcher.** Windows releases it
when the process ends, however it ends, so there is no stale lock to clean.
The state file never says whether a launcher is up: one whose lock is free was
left by a launcher that is gone, and is read for two things only -- the port of
the last start that reached `ready` (section 5.4), and whether the cluster that
may still be up was started by a launcher (section 7.3).

Per data root, and not per user, because the data root is what cannot have two
owners: the cluster, the logs, the thumbnails. A checkout (`SolidVision-dev`)
and an installed app (`SolidVision`) run side by side, each with its own.

Nothing here imports the `Settings` or the logger: the second launcher reads
the state file and leaves without either (section 8). Its records go to the
standard logger of its name, which the app's logger takes over once configured.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("app.app.launcher.instance")

LOCK_NAME = "launcher.lock"
STATE_NAME = "launcher.json"
STATE_FORMAT = 1
EVENT_PREFIX = "Local\\SolidVision-launcher-"

ADMINISTRATORS_SID = "S-1-5-32-544"

LOCK_ATTEMPTS = 4
LOCK_PAUSE_SECONDS = 0.05
"""A busy lock is tried again for 150 ms before it means "another launcher".

`status` and `stop` take the lock for a moment to see whether it is held, and
a launcher starting in that moment would otherwise take that for another one.
"""

REPLACE_SECONDS = 2.0
"""How long a rename of the state file is retried while a reader holds it.

Python opens files without `FILE_SHARE_DELETE`, so a `status` reading the
state at that instant makes Windows refuse to replace it.
"""

_WAIT_OBJECT_0 = 0
_EVENT_MODIFY_STATE = 0x0002


class LauncherState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    """Written by an ordered stop: no launcher, and nothing of one left up."""


class ClusterArrival(StrEnum):
    """How the cluster came to the launcher (section 7.3)."""

    STARTED = "started"
    """A launcher started it: a launcher stops it."""
    ADOPTED = "adopted"
    """Someone else started it -- `embedded start`, `run --`: left up."""
    EXTERNAL = "external"
    """The `external` source: the launcher touches no database (section 9)."""


@dataclass(frozen=True)
class InstanceRecord:
    """What `launcher.json` says."""

    state: LauncherState
    pid: int | None = None
    url: str | None = None
    children: Mapping[str, int] = field(default_factory=dict)
    cluster: ClusterArrival | None = None
    postmaster_pid: int | None = None
    """The server the launcher started or adopted, so that a launcher that
    inherits a cluster inherits that one, and not a server somebody started
    by hand since (section 7.3)."""
    last_ready_port: int | None = None
    """The port of the last start that reached `ready` (section 5.4)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "format": STATE_FORMAT,
            "state": self.state.value,
            "pid": self.pid,
            "url": self.url,
            "children": dict(self.children),
            "cluster": self.cluster.value if self.cluster else None,
            "postmaster_pid": self.postmaster_pid,
            "last_ready_port": self.last_ready_port,
        }

    @classmethod
    def from_json(cls, raw: object) -> InstanceRecord:
        """The record, or `ValueError` for anything this version did not write."""
        if not isinstance(raw, dict) or raw.get("format") != STATE_FORMAT:
            raise ValueError("not a launcher state of this format")
        children = raw.get("children") or {}
        if not isinstance(children, dict):
            raise ValueError("children is not an object")
        cluster = raw.get("cluster")
        return cls(
            state=LauncherState(raw["state"]),
            pid=_optional_int(raw.get("pid")),
            url=str(raw["url"]) if raw.get("url") is not None else None,
            children={str(name): int(pid) for name, pid in children.items()},
            cluster=ClusterArrival(cluster) if cluster is not None else None,
            postmaster_pid=_optional_int(raw.get("postmaster_pid")),
            last_ready_port=_optional_int(raw.get("last_ready_port")),
        )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{value!r} is not an integer")
    return value


class StateFile:
    """`launcher.json`, read leniently and written in one rename."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> InstanceRecord | None:
        """The record, or `None` when there is none this version can read.

        A file that cannot be parsed is treated as absent: the worst that
        costs is a new port, and a cluster left up instead of stopped.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            logger.warning("%s could not be read (%s); ignored", self._path, error)
            return None
        try:
            return InstanceRecord.from_json(raw)
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(
                "%s is not a state this version wrote (%s)", self._path, error
            )
            return None

    def write(self, record: InstanceRecord) -> bool:
        """Replace the file with `record`, so that no reader sees half of one.

        A state that cannot be written is logged and otherwise ignored: the
        launcher works without it, and the next start chooses a new port.
        """
        temporary = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(record.to_json(), indent=2) + "\n", encoding="utf-8"
            )
            deadline = time.monotonic() + REPLACE_SECONDS
            while True:
                try:
                    os.replace(temporary, self._path)
                    return True
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except OSError as error:
            logger.warning("%s could not be written (%s)", self._path, error)
            temporary.unlink(missing_ok=True)
            return False


class InstanceLock:
    """`launcher.lock`, locked with `msvcrt.locking`, like the adapter's lock.

    Held for the life of the launcher, never released while it runs. Windows
    releases it when the process ends, killed or not.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def acquire(
        self, attempts: int = LOCK_ATTEMPTS, pause: float = LOCK_PAUSE_SECONDS
    ) -> bool:
        """Take the lock, or report that another process holds it."""
        import msvcrt

        if self._descriptor is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        for attempt in range(attempts):
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                if attempt + 1 < attempts:
                    time.sleep(pause)
                continue
            self._descriptor = descriptor
            return True
        os.close(descriptor)
        return False

    def release(self) -> None:
        import msvcrt

        if self._descriptor is None:
            return
        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def held_elsewhere(self) -> bool:
        """Whether a launcher holds the lock now; takes it for an instant."""
        if self._descriptor is not None:
            return False
        if self.acquire(attempts=1):
            self.release()
            return False
        return True


@dataclass(frozen=True)
class Instance:
    """The three things of one data root, by their paths and name."""

    data_directory: Path

    @property
    def lock(self) -> Path:
        return self.data_directory / LOCK_NAME

    @property
    def state(self) -> Path:
        return self.data_directory / STATE_NAME

    @property
    def stop_event_name(self) -> str:
        """Per session (`Local\\`) and per data root: the `stop` of a checkout
        does not stop the installed app."""
        key = os.path.normcase(str(self.data_directory)).encode("utf-8")
        return EVENT_PREFIX + hashlib.sha256(key).hexdigest()[:16]


class StopRequests:
    """Every way a stop is asked, counted where the supervisor can poll them.

    `ask()` is a Ctrl+C, the end of stdin or a `stop`; the second one during
    a stop skips the deadlines (section 7.2). `end_session()` is the window of
    section 7.5: it shortens the deadlines and never skips them, because what
    the deadlines protect -- the job going back to the queue -- is the point of
    stopping in order at a logoff at all.

    Here, before any `Settings`: the signal handler that feeds it is installed
    before the lock is even tried. A re-entrant lock, because that handler
    runs in the main thread between any two instructions -- including those
    of a `count()` that already holds the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._count = 0
        self._session_ending = False
        self._sources: list[Callable[[], bool]] = []

    def ask(self) -> None:
        with self._lock:
            self._count += 1

    def end_session(self) -> None:
        with self._lock:
            self._session_ending = True

    def poll(self, source: Callable[[], bool]) -> None:
        """Add a source that says, when asked, whether a request arrived."""
        self._sources.append(source)

    def count(self) -> int:
        for source in self._sources:
            if source():
                self.ask()
        with self._lock:
            return self._count

    def session_ending(self) -> bool:
        with self._lock:
            return self._session_ending


class StopEvent:
    """The named event the launcher waits on, and `stop` signals.

    Auto-reset: each signal is one request. The second request during a stop
    is the order that skips the deadlines (section 7.2).
    """

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def create(cls, name: str) -> StopEvent:
        kernel32 = _kernel32()
        handle = kernel32.CreateEventW(None, False, False, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), f"Could not create the event {name}")
        # A signal left from before -- a `stop` that arrived as the previous
        # launcher was leaving -- is not addressed to this one.
        kernel32.ResetEvent(handle)
        return cls(handle)

    def is_set(self) -> bool:
        """Whether a request arrived since the last look; consumes it."""
        if self._handle is None:
            return False
        return bool(_kernel32().WaitForSingleObject(self._handle, 0) == _WAIT_OBJECT_0)

    def close(self) -> None:
        if self._handle is not None:
            _kernel32().CloseHandle(self._handle)
            self._handle = None

    @staticmethod
    def signal(name: str) -> bool:
        """Ask the launcher of `name` to stop. `False` when there is none."""
        kernel32 = _kernel32()
        handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)


def administrators_enabled() -> bool | None:
    """Whether the Administrators group is in force in this process's token.

    The group, and not `TokenElevation`, which RFC-033 section 8.3 measured
    reading `True` even in the token `pg_ctl` restricts. `CheckTokenMembership`
    answers yes only for a group that is enabled, not one kept to deny. `None`
    when Windows would not say.
    """
    from ctypes import wintypes

    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = _kernel32()
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.CheckTokenMembership.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
    ]
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(ADMINISTRATORS_SID, ctypes.byref(sid)):
        return None
    try:
        member = wintypes.BOOL()
        if not advapi32.CheckTokenMembership(None, sid, ctypes.byref(member)):
            return None
        return bool(member.value)
    finally:
        kernel32.LocalFree(sid)


def _kernel32() -> Any:
    """`kernel32` with the signatures this module calls, set every time.

    Typed `Any` for the reason `windows_processes.py` gives: a `ctypes` DLL
    has no static signature, and the `argtypes` keep 64-bit handles whole.
    """
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return kernel32
