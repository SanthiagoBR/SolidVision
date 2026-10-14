"""Where things are: the mode, the root and the user's data directory (RFC-034 4).

The only module of `backend/app` that uses `__file__`, the working directory
or `LOCALAPPDATA`; `tests/test_path_anchors_architecture.py` keeps it that
way. It imports neither `settings` nor the logger, and both import it: the
layout is the geometry of directories, not configuration.

The tree is the same in both modes (section 4.1)::

    <root>\\
        backend\\           app\\, alembic\\, alembic.ini
        runtime\\postgres\\  RFC-033
        pyproject.toml     only in a checkout
        .env               only in a checkout

A `Layout` can be built by hand, with any root and any data directory. That is
how the tests exercise the installed mode from inside a checkout, with no
global state.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

CHECKOUT_MARKER = "pyproject.toml"
"""What makes a directory a checkout: `pytest`, `mypy` and `ruff` all depend
on it, and an installer has no reason to carry it. `.git` would not do -- an
exported checkout has none (section 4.2)."""

INSTALLED_DATA_NAME = "SolidVision"
DEVELOPMENT_DATA_NAME = "SolidVision-dev"
"""A sibling of the installed one, not a folder inside it: anything a future
uninstaller does to the user's data must not reach a developer's (section 6.2)."""

CHECKOUT_CONFIG_NAME = ".env"
USER_CONFIG_NAME = "solidvision.env"


class Environment(StrEnum):
    """The mode. A fact read from the disk, never a value someone declares."""

    DEVELOPMENT = "development"
    INSTALLED = "installed"


@dataclass(frozen=True)
class Layout:
    """The mode, and the two directories everything else is derived from."""

    environment: Environment
    root: Path
    """The repository root, or the installation's."""
    data_directory: Path
    """`%LOCALAPPDATA%\\SolidVision-dev`, or `%LOCALAPPDATA%\\SolidVision`."""

    def __post_init__(self) -> None:
        for name in ("root", "data_directory"):
            path: Path = getattr(self, name)
            if not path.is_absolute():
                raise ValueError(f"A layout's {name} must be absolute, not {path}.")

    # Everything below is derived, and none of it touches the disk.

    @property
    def config_file(self) -> Path:
        """The one file this mode reads, and never the other mode's (section 5.1)."""
        if self.environment is Environment.DEVELOPMENT:
            return self.root / CHECKOUT_CONFIG_NAME
        return self.data_directory / USER_CONFIG_NAME

    @property
    def config_base(self) -> Path:
        """The folder of `config_file`: what a relative path setting is
        relative to (section 6.3, rule 4)."""
        return self.config_file.parent

    @property
    def backend_directory(self) -> Path:
        """Where `alembic.ini` and `alembic\\` are."""
        return self.root / "backend"

    @property
    def postgres_runtime(self) -> Path:
        """The vendored PostgreSQL binaries (RFC-033 section 5.2)."""
        return self.root / "runtime" / "postgres"

    @property
    def log_directory(self) -> Path:
        return self.data_directory / "logs"

    @property
    def thumbnail_directory(self) -> Path:
        return self.data_directory / "thumbnails"

    @property
    def postgres_directory(self) -> Path:
        """The user's cluster; the adapter adds the major version below it."""
        return self.data_directory / "postgres"


def layout_at(root: Path, profile: Path) -> Layout:
    """The layout of the code under `root`, for a user whose profile is `profile`.

    `development` needs proof and `installed` is what is left, because the two
    mistakes do not cost the same (section 4.2). A user's machine taken for a
    checkout would read the environment and write to `SolidVision-dev`, and the
    day the detection was fixed the user's data would seem to have vanished:
    silent and expensive. A checkout taken for an installation ignores its
    `.env` and looks for an embedded cluster, and the developer notices on the
    first run.
    """
    if (root / CHECKOUT_MARKER).is_file():
        return Layout(Environment.DEVELOPMENT, root, profile / DEVELOPMENT_DATA_NAME)
    return Layout(Environment.INSTALLED, root, profile / INSTALLED_DATA_NAME)


def local_app_data(environ: Mapping[str, str] | None = None) -> Path:
    """The per-user, per-machine application data folder (section 6.2).

    The variable first: it is how a validation run points a process at a
    temporary directory without touching the real profile. Without it -- a
    launcher may build its children's environment from nothing -- Windows is
    asked, rather than each default guessing a different fallback.
    """
    source = os.environ if environ is None else environ
    variable = source.get("LOCALAPPDATA", "")
    if variable and Path(variable).is_absolute():
        return Path(variable)
    if sys.platform != "win32":
        # One root, for a process that will not get far here anyway: the
        # volume adapter and the embedded cluster are both Windows-only.
        return Path.home() / ".local" / "share"
    return _known_local_app_data()


def _known_local_app_data() -> Path:
    """`FOLDERID_LocalAppData`, asked of the shell."""
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder = Guid(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )
    shell32: Any = ctypes.WinDLL("shell32")
    ole32: Any = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    # An `HRESULT` return type makes ctypes raise `OSError` on a failure.
    shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.c_void_p()
    shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer))
    try:
        if pointer.value is None:
            raise OSError("Windows named no folder for FOLDERID_LocalAppData.")
        return Path(ctypes.wstring_at(pointer.value))
    finally:
        ole32.CoTaskMemFree(pointer)


def detect_layout() -> Layout:
    """The layout of the code that is running.

    The one place that computes the root, and so the one function to change
    for a frozen app, where the root comes from the executable's path instead
    of from `__file__` (section 4.1). With an embedded Python and the code in
    a tree, `__file__` keeps working and nothing changes.
    """
    return layout_at(Path(__file__).resolve().parents[4], local_app_data())


layout = detect_layout()
