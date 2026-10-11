"""Paths as a PostgreSQL program can receive them (RFC-033 section 10).

`initdb`, `pg_ctl` and `postgres` are C programs with a narrow `main`: on
Windows they get their arguments in the ANSI code page. A path with a
character outside it does not arrive. Measured on a code page 1252 machine
with a directory named `Łukasz 東京`: `initdb` was handed `Lukasz ??` -- the
`Ł` replaced by its best-fit `L`, which names *another* directory, and the
ideographs by `?` -- and failed looking for the password file there. With the
runtime itself under such a directory, `initdb` could not even find its own
executable.

`João Silva` is not this problem: every character of it is in code page
1252, and it worked untouched.

So the second branch RFC-033 wrote before measuring is the one in force. A
path that does not fit is replaced by its 8.3 short form, which is ASCII and
names the same directory; `initdb` ran through one in the same measurement.
A volume that generates no short names leaves nothing to hand over, and the
adapter refuses, naming the setting that moves the directory elsewhere.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

from .errors import EmbeddedPostgresError

_BUFFER_LENGTH = 32768


def fits_ansi_code_page(text: str) -> bool:
    """Whether every character survives the trip through the ANSI code page.

    `mbcs` in strict mode refuses best-fit substitutions, so `Ł` on a 1252
    machine is a "no" rather than a silent `L`.
    """
    if sys.platform != "win32":
        return True
    try:
        text.encode("mbcs")
    except UnicodeEncodeError:
        return False
    return True


def short_path(path: Path) -> str:
    """`GetShortPathNameW`: the 8.3 form, or `""` when Windows gives none.

    The path must exist. On a volume without short names the call succeeds
    and answers with the long name unchanged, which the caller tells apart by
    testing the answer, not the return code.
    """
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    buffer = ctypes.create_unicode_buffer(_BUFFER_LENGTH)
    if not kernel32.GetShortPathNameW(str(path), buffer, _BUFFER_LENGTH):
        return ""
    return buffer.value


def argv_safe(path: Path, *, setting: str) -> Path:
    """`path`, spelt so that it reaches a narrow-`main` program intact.

    Unchanged when it already fits the code page; its short form when it does
    not; and a refusal that names `setting` when there is no short form.
    """
    if fits_ansi_code_page(str(path)):
        return path
    short = short_path(path)
    if short and fits_ansi_code_page(short):
        return Path(short)
    code_page = ctypes.WinDLL("kernel32").GetACP()
    raise EmbeddedPostgresError(
        f"{path} has characters outside this Windows' ANSI code page "
        f"({code_page}). PostgreSQL's programs cannot be given such a path, and "
        "this volume has no 8.3 short name to give them instead. Point "
        f"{setting} at a directory whose path fits the code page."
    )
