"""The user's cluster directory: paths, DACL, password, transition lock.

RFC-033 sections 6, 7.1 and 7.2. The layout, under `%LOCALAPPDATA%` by
default (the `embedded_postgres_directory` setting)::

    postgres\\                   explicit DACL: the user and SYSTEM
        16\\
            data\\               PGDATA
            password            one line
            adapter.lock        transition lock (section 7.1)
            startup.log         `pg_ctl -l`, rewritten on every start (8.1)
            pg_ctl.log          `pg_ctl`'s own output, rewritten on every call

The major version is in the path so that the `17\\` of a future upgrade fits
beside `16\\` without renaming anything: `pg_upgrade` needs both sides at once.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .code_page import argv_safe
from .errors import EmbeddedPostgresError

SYSTEM_SID = "S-1-5-18"
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class ClusterFiles:
    """Every path of one major version's cluster, and nothing else."""

    directory: Path

    @property
    def data(self) -> Path:
        return self.directory / "data"

    @property
    def data_initdb(self) -> Path:
        """Where `initdb` writes; renamed to `data` only when it succeeds (7.1)."""
        return self.directory / "data.initdb"

    @property
    def password(self) -> Path:
        return self.directory / "password"

    @property
    def password_tmp(self) -> Path:
        return self.directory / "password.tmp"

    @property
    def lock(self) -> Path:
        return self.directory / "adapter.lock"

    @property
    def startup_log(self) -> Path:
        return self.directory / "startup.log"

    @property
    def pg_ctl_log(self) -> Path:
        return self.directory / "pg_ctl.log"

    @property
    def postmaster_pid(self) -> Path:
        return self.data / "postmaster.pid"

    @property
    def pg_version(self) -> Path:
        return self.data / "PG_VERSION"

    @property
    def pg_control(self) -> Path:
        return self.data / "global" / "pg_control"

    @property
    def postgresql_conf(self) -> Path:
        return self.data / "postgresql.conf"

    @property
    def server_log_directory(self) -> Path:
        """Where the logging collector writes: one file per weekday (8.1)."""
        return self.data / "log"

    @property
    def server_conf(self) -> Path:
        """`solidvision.conf`, included by `postgresql.conf` (section 8.1)."""
        return self.data / "solidvision.conf"


class DirectoryAcl(ABC):
    """Port for restricting a directory to the current user and SYSTEM."""

    @abstractmethod
    def restrict_to_current_user(self, directory: Path) -> None:
        """Replace inheritance with full control for the user's SID and SYSTEM."""


class IcaclsDirectoryAcl(DirectoryAcl):
    """`icacls.exe` from `System32`, by absolute path, with SIDs, not names.

    In a Portuguese Windows the `Everyone` group is called `Todos`: a
    translated name must not decide a permission, so the user is named by the
    SID read from the process token (RFC-033 section 6).
    """

    def restrict_to_current_user(self, directory: Path) -> None:
        sid = current_user_sid()
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        completed = subprocess.run(
            [
                str(system_root / "System32" / "icacls.exe"),
                str(directory),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                f"*{SYSTEM_SID}:(OI)(CI)F",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=60,
        )
        if completed.returncode != 0:
            output = (completed.stdout + completed.stderr).decode("oem", "replace")
            raise EmbeddedPostgresError(
                f"icacls could not restrict {directory} "
                f"(exit {completed.returncode}): {output.strip()}"
            )


def current_user_sid() -> str:
    """The string SID of the user this process runs as, read from its token."""
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
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
    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise EmbeddedPostgresError(
            "Could not open the process token "
            f"(Windows error {ctypes.get_last_error()})."
        )
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, token_user, buffer, size, ctypes.byref(size)
        ):
            raise EmbeddedPostgresError(
                "Could not read the token user "
                f"(Windows error {ctypes.get_last_error()})."
            )
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, which starts with the PSID.
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        string_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(string_sid)):
            raise EmbeddedPostgresError(
                "Could not format the user SID "
                f"(Windows error {ctypes.get_last_error()})."
            )
        try:
            return str(string_sid.value)
        finally:
            kernel32.LocalFree(string_sid)
    finally:
        kernel32.CloseHandle(token)


class ClusterHome:
    """The per-user directory every cluster of every major version lives under."""

    LOCK_POLL_SECONDS = 0.1

    def __init__(self, root: Path, *, acl: DirectoryAcl | None = None) -> None:
        if not root.is_absolute():
            raise EmbeddedPostgresError(
                f"The cluster directory must be absolute, not {root}."
            )
        self._root = root
        self._acl = acl if acl is not None else IcaclsDirectoryAcl()

    @property
    def root(self) -> Path:
        return self._root

    def files(self, major: int) -> ClusterFiles:
        return ClusterFiles(self._root / str(major))

    def ensure_directory(self, files: ClusterFiles) -> None:
        """Create `postgres\\` and the version directory, the lock's home."""
        files.directory.mkdir(parents=True, exist_ok=True)

    def for_commands(self, files: ClusterFiles) -> ClusterFiles:
        """The same files, by a path `initdb` and `pg_ctl` can be given.

        Section 10: under a user directory outside the ANSI code page, the
        8.3 short form of the version directory. Everything below it has an
        ASCII name of the adapter's own choosing. A directory that does not
        exist yet has no short form, and nothing to run in it either.
        """
        if not files.directory.exists():
            return files
        return ClusterFiles(
            argv_safe(files.directory, setting="EMBEDDED_POSTGRES_DIRECTORY")
        )

    def secure(self) -> None:
        """Apply the explicit DACL to `postgres\\` (section 6).

        Called on the first-run path, before the password is written, rather
        than only on the `mkdir` that creates the directory: a process killed
        between the two would otherwise leave the directory with the
        inherited DACL for ever. `icacls` is idempotent, and this runs once
        per cluster, beside an `initdb` that takes tens of seconds.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._acl.restrict_to_current_user(self._root)

    # -- the password (section 7.2) ---------------------------------------

    def read_password(self, files: ClusterFiles) -> str | None:
        try:
            password = files.password.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        return password or None

    def write_new_password(self, files: ClusterFiles) -> str:
        """Generate the cluster's password and write it before `initdb` runs.

        `token_urlsafe` matters for its alphabet (`A-Z a-z 0-9 _ -`):
        `Settings.database_url` interpolates the password into the URL
        without escaping anything, and an `@`, `:` or `/` would break the
        connection in a way that is hard to diagnose. Written to a temporary
        name and renamed, so a reader never sees half a password.
        """
        password = secrets.token_urlsafe(24)
        files.password_tmp.write_text(password + "\n", encoding="ascii")
        os.replace(files.password_tmp, files.password)
        return password

    def discard_password(self, files: ClusterFiles) -> None:
        """A password without a `data` is not half of a pair (section 7.1)."""
        files.password.unlink(missing_ok=True)
        files.password_tmp.unlink(missing_ok=True)

    # -- the transition lock (section 7.1) --------------------------------

    @contextmanager
    def transition_lock(self, files: ClusterFiles, timeout: float) -> Iterator[None]:
        """Hold `adapter.lock` exclusively for one creation, start or stop.

        Never held while the cluster lives: whoever arrives second waits and
        then adopts. `msvcrt.locking` is released by the operating system
        when the process dies, so a process killed in the middle of an
        `initdb` does not leave the directory locked.
        """
        import msvcrt

        self.ensure_directory(files)
        descriptor = os.open(files.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EmbeddedPostgresError(
                            f"Another process has held {files.lock} for more than "
                            f"{timeout:.0f} s; it may be creating or recovering "
                            "the cluster."
                        ) from None
                    time.sleep(self.LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)
