"""The name of the program this process is running (RFC-034 section 8.2).

Each program writes its own log file, named after it::

    python -m uvicorn app.presentation.api:app          uvicorn.log
    python -m app.infrastructure.workers.job_runner     job_runner.log
    python -m app.infrastructure.database.embedded ...  embedded.log
    python -m app.cli ...                               cli.log
    python -m pytest, or pytest                         pytest.log
    anything else (python -c, a REPL)                   python.log

Derived here and nowhere else, so that no entry point has to declare
anything -- which none could do in time: the logger is configured by the
first `get_logger()`, during import, before any `main()` runs.

For the same reason the name is read from `sys.orig_argv`, the command line
as the interpreter received it. While the packages of a `-m` module are
still being imported, `sys.argv[0]` is the literal `-m` and
`__main__.__spec__` is not set yet.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import PureWindowsPath

FALLBACK = "python"

_OPTIONS_WITH_A_VALUE = "WX"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def program_name(argv: Sequence[str] | None = None) -> str:
    """The module given to `-m`, or the script, or `python`."""
    arguments = list(sys.orig_argv if argv is None else argv)[1:]
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        if argument == "--check-hash-based-pycs":
            position += 2
            continue
        if argument.startswith("--"):
            position += 1
            continue
        if argument == "-":
            return FALLBACK
        if not argument.startswith("-"):
            return _clean(PureWindowsPath(argument).stem)
        # A cluster of one-letter options. `-m` and `-c` end the interpreter's
        # own options, and whatever follows the letter is their value.
        for index, letter in enumerate(argument[1:], start=1):
            if letter == "m":
                attached = argument[index + 1 :]
                if attached:
                    return _module(attached)
                following = arguments[position + 1 : position + 2]
                return _module(following[0]) if following else FALLBACK
            if letter == "c":
                return FALLBACK
            if letter in _OPTIONS_WITH_A_VALUE:
                if index == len(argument) - 1:
                    position += 1
                break
        position += 1
    return FALLBACK


def _module(name: str) -> str:
    parts = [part for part in name.split(".") if part and part != "__main__"]
    return _clean(parts[-1]) if parts else FALLBACK


def _clean(name: str) -> str:
    return _UNSAFE.sub("_", name) or FALLBACK
