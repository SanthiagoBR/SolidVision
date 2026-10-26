"""Which DLLs a tree of Windows binaries asks for, and where each one is found.

The PE reader of RFC-033 section 5.3, moved here from
`experiments/rfc-033-embedded-postgres/measure_adapter.py` for RFC-041 section
7.3: the package carries three runtimes, and a DLL that only this machine's
`System32` has -- the `msvcp140.dll` a redistributable put there, which
`torch` asks for (section 2.3) -- passes every test on this machine and fails
on the next one.

**A scope is a runtime**: a directory and every PE file under it, except
those under a nested scope. A name one of its files imports is found

1. in the scope: beside the importing file, at the scope's root -- the
   directory of `python.exe` is the application directory of every module the
   interpreter loads --, or in any directory of the scope that holds a DLL,
   which is what the packages' `os.add_dll_directory()` calls amount to
   (`torch\\lib`, `numpy.libs`);
2. among Windows' own: an API set (`api-ms-win-*`, `ext-ms-*`), a name of
   `KnownDLLs`, or a file of `System32` under Windows Resource Protection.

Anything else is missing. A file of `System32` that Windows does not protect
is missing too, and is named as such: it is what the check exists to catch.
Never the other scopes: `runtime\\postgres` holds an `msvcp140.dll` that no
Python process will ever look for.

`ctypes.CDLL("name")` appears in no import table. Those names are given to the
check by hand, per scope (`torch`'s three, section 2.3).

Standard library only: it runs from the development virtualenv, and its
reader is pure `struct`.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PE_SUFFIXES = (".exe", ".dll", ".pyd")
API_SET_PREFIXES = ("api-ms-win-", "ext-ms-")


class NotAPeFileError(ValueError):
    """The file has a PE suffix and is not one."""


def pe_imports(path: Path) -> list[str]:
    """The DLL names a PE file imports, load-time and delay-loaded."""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise NotAPeFileError(f"{path} is not a PE file")
    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise NotAPeFileError(f"{path} is not a PE file")
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
        raise NotAPeFileError(f"{path}: RVA {rva:#x} is in no section")

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


# --------------------------------------------------------------------------
# Windows' own DLLs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Windows:
    """What the loader takes from Windows itself. Replaceable in the tests."""

    system32: Path
    known: frozenset[str]
    """`KnownDLLs`, lower case."""
    protects: Callable[[Path], bool]
    """Whether Windows Resource Protection covers a file: it is Windows' own."""

    def provides(self, name: str) -> str | None:
        """How Windows provides `name`, or None when it does not."""
        lower = name.lower()
        if lower.startswith(API_SET_PREFIXES):
            return "api set"
        if lower in self.known:
            return "KnownDLLs"
        candidate = self.system32 / name
        if candidate.is_file() and self.protects(candidate):
            return "System32, protected"
        return None

    def unprotected(self, name: str) -> bool:
        """`System32` has it, and Windows does not call it its own."""
        candidate = self.system32 / name
        return candidate.is_file() and not self.protects(candidate)


def this_windows() -> Windows:
    """The running system's `System32`, `KnownDLLs` and resource protection."""
    if sys.platform != "win32":
        raise OSError("The DLLs of the package are checked on Windows.")
    root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    return Windows(root / "System32", frozenset(known_dlls()), windows_protects)


def windows_protects(path: Path) -> bool:
    """Whether Windows Resource Protection covers the file: it is Windows' own.

    Asked of Windows rather than read from the product name, which is
    translated -- "Sistema Operacional Microsoft® Windows®" on the machine
    this was written on. A `vcruntime140.dll` that a redistributable put in
    `System32` is not protected; `ucrtbase.dll`, part of Windows since 10, is.
    """
    from ctypes import wintypes

    sfc: Any = ctypes.WinDLL("sfc")
    sfc.SfcIsFileProtected.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
    return bool(sfc.SfcIsFileProtected(None, str(path)))


def known_dlls() -> set[str]:
    """`KnownDLLs`: names the loader takes from System32 before looking."""
    import winreg

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
# Resolving a scope
# --------------------------------------------------------------------------


@dataclass
class Missing:
    """A name nothing in the scope nor in Windows provides."""

    name: str
    wanted_by: list[str]
    """The files that import it, relative to the scope; `ctypes` for a name
    given by hand."""
    unprotected_in_system32: bool
    """Only this machine's `System32` has it: the case of section 2.3."""

    def line(self) -> str:
        where = (
            "only in this machine's System32, which Windows does not protect"
            if self.unprotected_in_system32
            else "nowhere"
        )
        users = ", ".join(self.wanted_by[:3])
        more = f" and {len(self.wanted_by) - 3} more" if len(self.wanted_by) > 3 else ""
        return f"{self.name}: {where}; wanted by {users}{more}"


@dataclass
class ScopeReport:
    root: Path
    files: int = 0
    from_scope: dict[str, str] = field(default_factory=dict)
    """Name -> where in the scope it was found, relative."""
    from_windows: dict[str, str] = field(default_factory=dict)
    """Name -> how Windows provides it."""
    missing: list[Missing] = field(default_factory=list)


def pe_files(root: Path, excluded: Iterable[Path] = ()) -> list[Path]:
    """Every PE file under `root`, except under the `excluded` directories."""
    skip = [path.resolve() for path in excluded]
    found = []
    for directory, subdirectories, names in os.walk(root):
        here = Path(directory).resolve()
        subdirectories[:] = [
            name for name in subdirectories if (here / name).resolve() not in skip
        ]
        found.extend(
            Path(directory) / name
            for name in names
            if name.lower().endswith(PE_SUFFIXES)
        )
    return sorted(found)


def resolve_scope(
    root: Path,
    windows: Windows,
    *,
    excluded: Sequence[Path] = (),
    by_name: Iterable[str] = (),
) -> ScopeReport:
    """Every name the scope's PE files import, and where each is found."""
    files = pe_files(root, excluded)
    in_scope: dict[str, list[Path]] = {}
    for path in files:
        in_scope.setdefault(path.name.lower(), []).append(path)
    dll_directories = {path.parent for path in files if path.suffix.lower() == ".dll"}

    wanted: dict[str, list[str]] = {}
    for path in files:
        try:
            names = pe_imports(path)
        except (NotAPeFileError, struct.error):
            # A file named like a binary that is not one (a test fixture, a
            # stub): nothing loads it as a module.
            continue
        for name in names:
            wanted.setdefault(name.lower(), []).append(
                path.relative_to(root).as_posix()
            )
    for name in by_name:
        wanted.setdefault(name.lower(), []).append("ctypes")

    report = ScopeReport(root, files=len(files))
    for key, users in sorted(wanted.items()):
        found = _found_in_scope(key, in_scope, {root, *dll_directories})
        if found is not None:
            report.from_scope[key] = found.relative_to(root).as_posix()
            continue
        how = windows.provides(key)
        if how is not None:
            report.from_windows[key] = how
            continue
        report.missing.append(Missing(key, users, windows.unprotected(key)))
    return report


def _found_in_scope(
    name: str, in_scope: Mapping[str, list[Path]], directories: set[Path]
) -> Path | None:
    for candidate in in_scope.get(name, ()):
        if candidate.parent in directories:
            return candidate
    return None


def missing_names(
    root: Path,
    windows: Windows,
    *,
    excluded: Sequence[Path] = (),
    by_name: Iterable[str] = (),
) -> list[str]:
    """The names of a scope nothing provides, lower case."""
    report = resolve_scope(root, windows, excluded=excluded, by_name=by_name)
    return [missing.name for missing in report.missing]
