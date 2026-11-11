"""RFC-050 section 15.3: what `icacls /inheritance:r /grant:r` leaves, on the
runner and here. Standard library only, so the image's Python runs it.

    python experiments/rfc-050-suite-do-backend/probe_acl.py

Three tests failed on the runner and pass here: the two of
`test_embedded_cluster_home.py` that read the DACL back, and the staging
lock of `test_stage_installation.py`. All three restrict a folder the way
the product does, `icacls <folder> /inheritance:r /grant:r *<user>:...
*S-1-5-18:...`, and on the runner the Administrators (`BA`) and the Owner
Rights (`OW`) were still there afterwards. This probe makes the folder
each way, and prints, at each step, the owner and the DACL as SDDL:

- `pytest`: a parent made like pytest's `tmp_path` (`mkdir(mode=0o700)`,
  which since Python 3.12.4 gives it `D:P(SY)(BA)(OW)`), and a child;
- `product`: a parent made with the default mode under `%LOCALAPPDATA%`,
  where `SolidVision-dev` lives, and a child;
- each child restricted in one call, as `IcaclsDirectoryAcl` does, and in
  two (`/inheritance:r`, then `/grant:r`), to see which step leaves what;
- the staging lock (`RX` for the user), and whether this process can still
  write a file in it;
- the fix of section 16: the same two DACLs set by `SetNamedSecurityInfoW`
  with a protected DACL, the lock checked on a folder below too, and given
  back by `icacls /reset`, as `unlock()` does.

Prints the token's user, groups and integrity level (`whoami`), and, last,
one line `probe_acl.json: {...}` with everything.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

SYSTEM_SID = "S-1-5-18"
ICACLS = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe")

advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.ULONG),
]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def sddl(path: Path) -> str:
    """Owner and DACL of `path` as SDDL."""
    information = 0x1 | 0x4  # OWNER, DACL
    descriptor = ctypes.c_void_p()
    error = advapi32.GetNamedSecurityInfoW(
        str(path), 1, information, None, None, None, None, ctypes.byref(descriptor)
    )
    if error:
        return f"GetNamedSecurityInfoW failed with {error}"
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, information, ctypes.byref(text), None
        ):
            return f"Convert failed with {ctypes.get_last_error()}"
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(descriptor)


advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.ULONG),
]
advapi32.GetSecurityDescriptorDacl.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.BOOL),
]
advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD


def set_protected_dacl(path: Path, entries: tuple[str, ...]) -> str:
    """The fix of section 16, as cluster_home.set_protected_dacl does it
    (copied: this probe runs without the venv)."""
    descriptor = ctypes.c_void_p()
    sddl_text = "D:P" + "".join(f"({entry})" for entry in entries)
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl_text, 1, ctypes.byref(descriptor), None
    ):
        return f"convert failed with {ctypes.get_last_error()}"
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        )
        error = advapi32.SetNamedSecurityInfoW(
            str(path), 1, 0x4 | 0x80000000, None, None, dacl, None
        )
        return f"error {error}" if error else "set"
    finally:
        kernel32.LocalFree(descriptor)


def run(*command: str) -> str:
    done = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL)
    return f"exit {done.returncode}: " + (done.stdout + done.stderr).decode("oem", "replace").strip()


def user_sid() -> str:
    line = run("whoami", "/user", "/fo", "csv", "/nh")
    return line.rsplit(",", 1)[1].strip().strip('"')


def can_write(folder: Path) -> str:
    try:
        (folder / "probe-write.txt").write_text("x", encoding="ascii")
    except PermissionError as error:
        return f"denied ({error.winerror})"
    (folder / "probe-write.txt").unlink()
    return "written"


def case(parent_maker: str, base: Path, sid: str) -> dict[str, Any]:
    parent = base / "parent"
    if parent_maker == "pytest":
        parent.mkdir(mode=0o700)
    else:
        parent.mkdir()
    found: dict[str, Any] = {"parent": sddl(parent)}

    one = parent / "one-call"
    one.mkdir()
    found["child"] = sddl(one)
    found["one_call_icacls"] = run(
        ICACLS, str(one), "/inheritance:r", "/grant:r",
        f"*{sid}:(OI)(CI)F", f"*{SYSTEM_SID}:(OI)(CI)F",
    )
    found["one_call"] = sddl(one)
    (one / "16").mkdir()
    (one / "16" / "password").write_text("x", encoding="ascii")
    found["one_call_file_below"] = sddl(one / "16" / "password")

    two = parent / "two-calls"
    two.mkdir()
    found["two_calls_inheritance"] = run(ICACLS, str(two), "/inheritance:r")
    found["after_inheritance_r"] = sddl(two)
    found["two_calls_grant"] = run(
        ICACLS, str(two), "/grant:r", f"*{sid}:(OI)(CI)F", f"*{SYSTEM_SID}:(OI)(CI)F"
    )
    found["after_grant_r"] = sddl(two)

    locked = parent / "locked"
    locked.mkdir()
    found["lock_icacls"] = run(
        ICACLS, str(locked), "/inheritance:r", "/grant:r",
        f"*{sid}:(OI)(CI)RX", f"*{SYSTEM_SID}:(OI)(CI)F",
    )
    found["locked"] = sddl(locked)
    found["locked_write"] = can_write(locked)
    run(ICACLS, str(locked), "/reset")

    api = parent / "api"
    api.mkdir()
    found["api_call"] = set_protected_dacl(
        api, (f"A;OICI;FA;;;{sid}", f"A;OICI;FA;;;{SYSTEM_SID}")
    )
    found["api"] = sddl(api)
    (api / "16").mkdir()
    (api / "16" / "password").write_text("x", encoding="ascii")
    found["api_file_below"] = sddl(api / "16" / "password")

    api_locked = parent / "api-locked"
    api_locked.mkdir()
    (api_locked / "inside").mkdir()
    found["api_lock_call"] = set_protected_dacl(
        api_locked, (f"A;OICI;0x1200a9;;;{sid}", f"A;OICI;FA;;;{SYSTEM_SID}")
    )
    found["api_locked"] = sddl(api_locked)
    found["api_locked_inside"] = sddl(api_locked / "inside")
    found["api_locked_write"] = can_write(api_locked)
    found["api_locked_write_inside"] = can_write(api_locked / "inside")
    found["api_unlock"] = run(ICACLS, str(api_locked), "/reset")
    found["api_unlocked_write_inside"] = can_write(api_locked / "inside")
    return found


def main() -> int:
    sid = user_sid()
    result: dict[str, Any] = {
        "python": sys.version.split()[0],
        "whoami_user": run("whoami", "/user", "/fo", "csv", "/nh"),
        "user_sid": sid,
        "whoami_groups": [
            line.strip()
            for line in run("whoami", "/groups", "/fo", "csv", "/nh").splitlines()
            if any(word in line for word in ("S-1-5-32-544", "S-1-16-", "Administrators"))
        ],
        "cases": {},
    }
    # Another user started by Start-Process -Credential inherits this
    # environment: its own TEMP and LOCALAPPDATA come as arguments.
    roots = {
        "pytest": Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir()),
        "product": Path(sys.argv[2] if len(sys.argv) > 2 else os.environ["LOCALAPPDATA"]),
    }
    print("whoami:", result["whoami_user"], flush=True)
    for line in result["whoami_groups"]:
        print("  group:", line, flush=True)
    for name, root in roots.items():
        root.mkdir(parents=True, exist_ok=True)
        if name == "pytest":
            base = Path(tempfile.mkdtemp(prefix="rfc050-probe-", dir=root))
        else:  # the default mode, as the app makes SolidVision-dev
            base = root / f"rfc050-probe-{uuid.uuid4().hex[:8]}"
            base.mkdir()
        try:
            result["cases"][name] = case(name, base, sid)
        finally:
            subprocess.run([ICACLS, str(base), "/reset", "/t", "/q"], capture_output=True)
            shutil.rmtree(base, ignore_errors=True)
        print(f"== {name} (in {root})", flush=True)
        for key, value in result["cases"][name].items():
            print(f"  {key:<24} {value}", flush=True)
    print("probe_acl.json: " + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
