"""The user's cluster directory (RFC-033 sections 6, 7.1 and 7.2).

Two of these tests call Windows for real, on a temporary directory: the DACL
is applied by the real `icacls.exe` and read back as SDDL through `advapi32`,
and the lock is taken by a second process. Everything a double could stand in
for would be the part that can be wrong.
"""

from __future__ import annotations

import ctypes
import re
import string
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from app.infrastructure.database.embedded.cluster_home import (
    SYSTEM_SID,
    ClusterHome,
    DirectoryAcl,
    IcaclsDirectoryAcl,
    current_user_sid,
)
from app.infrastructure.database.embedded.errors import EmbeddedPostgresError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
)


class RecordingAcl(DirectoryAcl):
    def __init__(self) -> None:
        self.restricted: list[Path] = []

    def restrict_to_current_user(self, directory: Path) -> None:
        self.restricted.append(directory)


def make_home(tmp_path: Path) -> tuple[ClusterHome, RecordingAcl]:
    acl = RecordingAcl()
    return ClusterHome(tmp_path / "SolidVision" / "postgres", acl=acl), acl


def test_the_major_version_is_in_the_path(tmp_path: Path) -> None:
    """Section 6: `postgres\\17\\` must fit beside `postgres\\16\\` one day."""
    home, _ = make_home(tmp_path)

    files = home.files(16)

    assert files.directory == home.root / "16"
    assert files.data == home.root / "16" / "data"
    assert files.data_initdb == home.root / "16" / "data.initdb"
    assert files.password == home.root / "16" / "password"
    assert files.lock == home.root / "16" / "adapter.lock"
    assert files.startup_log == home.root / "16" / "startup.log"
    assert files.postmaster_pid == files.data / "postmaster.pid"
    assert files.server_conf == files.data / "solidvision.conf"
    assert home.files(17).data != files.data


def test_a_relative_cluster_directory_is_refused() -> None:
    with pytest.raises(EmbeddedPostgresError, match="absolute"):
        ClusterHome(Path("postgres"))


def test_securing_restricts_the_root_not_the_version_directory(tmp_path: Path) -> None:
    home, acl = make_home(tmp_path)

    home.secure()

    assert home.root.is_dir()
    assert acl.restricted == [home.root]


def test_the_password_is_one_line_in_a_url_safe_alphabet(tmp_path: Path) -> None:
    """Section 7.2. Nothing in it needs escaping, wherever it is put."""
    home, _ = make_home(tmp_path)
    files = home.files(16)
    home.ensure_directory(files)

    password = home.write_new_password(files)

    assert files.password.read_text(encoding="ascii") == password + "\n"
    assert set(password) <= set(string.ascii_letters + string.digits + "_-")
    assert len(password) == 32
    assert home.read_password(files) == password
    assert not files.password_tmp.exists()


def test_every_new_password_is_different(tmp_path: Path) -> None:
    home, _ = make_home(tmp_path)
    files = home.files(16)
    home.ensure_directory(files)

    assert home.write_new_password(files) != home.write_new_password(files)


def test_no_password_file_reads_as_none(tmp_path: Path) -> None:
    home, _ = make_home(tmp_path)

    assert home.read_password(home.files(16)) is None


def test_a_discarded_password_leaves_nothing_behind(tmp_path: Path) -> None:
    home, _ = make_home(tmp_path)
    files = home.files(16)
    home.ensure_directory(files)
    home.write_new_password(files)
    files.password_tmp.write_text("half", encoding="ascii")

    home.discard_password(files)

    assert not files.password.exists()
    assert not files.password_tmp.exists()


def test_the_lock_refuses_a_second_holder_until_the_first_lets_go(
    tmp_path: Path,
) -> None:
    """Section 7.1: whoever arrives second waits."""
    home, _ = make_home(tmp_path)
    files = home.files(16)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with home.transition_lock(files, timeout=5):
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(timeout=5)
        with pytest.raises(EmbeddedPostgresError, match="adapter.lock"):
            with home.transition_lock(files, timeout=0.3):
                pass
    finally:
        release.set()
        holder.join(timeout=10)

    with home.transition_lock(files, timeout=1):
        pass


def test_a_holder_that_dies_does_not_leave_the_directory_locked(
    tmp_path: Path,
) -> None:
    """Section 7.1: the operating system releases the lock with the process.

    The child takes the lock, says so, and is killed while holding it -- a
    process killed in the middle of an `initdb`.
    """
    home, _ = make_home(tmp_path)
    files = home.files(16)
    home.ensure_directory(files)
    script = textwrap.dedent("""
        import msvcrt, os, sys, time
        descriptor = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        print("locked", flush=True)
        time.sleep(60)
        """)
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(files.lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(EmbeddedPostgresError):
            with home.transition_lock(files, timeout=0.3):
                pass
    finally:
        child.kill()
        child.wait(timeout=10)

    with home.transition_lock(files, timeout=5):
        pass


def test_the_user_is_named_by_a_sid_read_from_the_token() -> None:
    """Section 6: a translated group name must not decide a permission."""
    assert re.fullmatch(r"S-1-\d+(-\d+)+", current_user_sid())


def test_icacls_leaves_only_the_user_and_system_and_cuts_inheritance(
    tmp_path: Path,
) -> None:
    """The real `icacls.exe`, read back as SDDL rather than as translated names.

    `D:P` is a protected DACL -- inheritance removed -- and each `(A;OICI;FA`
    is full control inherited by files and directories below. Two entries
    and no third is the assertion: the administrators are gone too.
    """
    directory = tmp_path / "postgres"
    directory.mkdir()

    IcaclsDirectoryAcl().restrict_to_current_user(directory)

    dacl = _dacl_sddl(directory)
    assert dacl.startswith("D:P")
    entries = set(re.findall(r"\(([^)]*)\)", dacl))
    assert entries == {
        "A;OICI;FA;;;SY",
        f"A;OICI;FA;;;{current_user_sid()}",
    }
    assert SYSTEM_SID == "S-1-5-18"


def test_a_file_created_below_inherits_the_same_two_entries(tmp_path: Path) -> None:
    """The password is written after the DACL, and gets it by inheritance."""
    directory = tmp_path / "postgres"
    directory.mkdir()
    IcaclsDirectoryAcl().restrict_to_current_user(directory)

    password = directory / "16" / "password"
    password.parent.mkdir()
    password.write_text("x", encoding="ascii")

    entries = set(re.findall(r"\(([^)]*)\)", _dacl_sddl(password)))
    assert entries == {"A;ID;FA;;;SY", f"A;ID;FA;;;{current_user_sid()}"}


def _dacl_sddl(path: Path) -> str:
    """The DACL of `path` as SDDL, which names accounts by SID."""
    from ctypes import wintypes

    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
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
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    se_file_object = 1
    dacl_security_information = 0x00000004
    descriptor = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        str(path),
        se_file_object,
        dacl_security_information,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    assert error == 0, f"GetNamedSecurityInfoW failed with {error}"
    try:
        text = wintypes.LPWSTR()
        assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, dacl_security_information, ctypes.byref(text), None
        )
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)
