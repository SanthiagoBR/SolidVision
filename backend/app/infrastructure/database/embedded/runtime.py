"""The vendored PostgreSQL binaries, and the environment they run in (RFC-033 5).

`scripts/vendor_postgres.py` materializes the runtime from a lockfile and
writes a manifest beside it. This module is the other half: it finds the
binaries by absolute path -- never through `PATH` -- checks that the manifest
and the four files the cluster needs are there, and builds the environment
every PostgreSQL child process gets.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .code_page import argv_safe
from .errors import RuntimeInvalidError

MANIFEST_NAME = "SOLIDVISION-RUNTIME.json"
MANIFEST_FORMAT = 1

REQUIRED_FILES = (
    "Library/bin/postgres.exe",
    "Library/bin/initdb.exe",
    "Library/bin/pg_ctl.exe",
    "Library/lib/vector.dll",
)
"""Checked for presence on every start; never hashed (RFC-033 section 5.2).

Hashing 123 MB off a hard disk on every start would cost more than the start
itself. The integrity of the files is the installer's responsibility.
"""

MATERIALIZE_HINT = "Materialize it with `python scripts/vendor_postgres.py`."


@dataclass(frozen=True)
class RuntimeManifest:
    """What `vendor_postgres.py` wrote: the versions the lockfile pins."""

    postgresql: str
    pgvector: str
    lockfile_sha256: str

    @property
    def major(self) -> int:
        """`16` for `16.15`: the part that names the cluster directory (section 6)."""
        return int(self.postgresql.split(".", 1)[0])


class PostgresRuntime:
    """One materialized runtime directory, refused unless it is complete."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise RuntimeInvalidError(
                f"The runtime directory must be absolute, not {root}: a relative "
                "one would move with the working directory."
            )
        self._root = root
        self._command_root: Path | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def command_root(self) -> Path:
        """The root as the executables themselves can be told it (section 10).

        `initdb` finds `postgres.exe` from its own path, which it reads in the
        ANSI code page; under a directory outside it, it cannot find itself.
        So the executables are started through the 8.3 short form of the
        root whenever the long one does not fit -- which makes the directory
        the installer chooses a restriction only on a volume without short
        names.
        """
        if self._command_root is None:
            self._command_root = (
                argv_safe(self._root, setting="EMBEDDED_POSTGRES_RUNTIME_DIRECTORY")
                if self._root.exists()
                else self._root
            )
        return self._command_root

    @property
    def bin_directory(self) -> Path:
        return self.command_root / "Library" / "bin"

    @property
    def initdb(self) -> Path:
        return self.bin_directory / "initdb.exe"

    @property
    def pg_ctl(self) -> Path:
        return self.bin_directory / "pg_ctl.exe"

    @property
    def postgres(self) -> Path:
        return self.bin_directory / "postgres.exe"

    def manifest(self) -> RuntimeManifest:
        """Read the manifest and check the four files, or refuse the directory.

        Cheap by design -- one small JSON file and four `stat`s -- because it
        runs on every start.
        """
        path = self._root / MANIFEST_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuntimeInvalidError(
                f"{self._root} has no {MANIFEST_NAME}, so it is not a runtime "
                f"this project materialized. {MATERIALIZE_HINT}"
            ) from None
        except (OSError, ValueError) as error:
            raise RuntimeInvalidError(
                f"{path} could not be read ({error}). {MATERIALIZE_HINT}"
            ) from error
        if not isinstance(raw, dict) or raw.get("format") != MANIFEST_FORMAT:
            raise RuntimeInvalidError(
                f"{path} is not a format-{MANIFEST_FORMAT} manifest. "
                f"{MATERIALIZE_HINT}"
            )
        try:
            manifest = RuntimeManifest(
                postgresql=str(raw["postgresql"]),
                pgvector=str(raw["pgvector"]),
                lockfile_sha256=str(raw["lockfile_sha256"]),
            )
            if manifest.major < 1:
                raise ValueError(f"postgresql version {manifest.postgresql!r}")
        except (KeyError, ValueError) as error:
            raise RuntimeInvalidError(
                f"{path} is incomplete ({error!r}). {MATERIALIZE_HINT}"
            ) from error
        missing = [name for name in REQUIRED_FILES if not (self._root / name).is_file()]
        if missing:
            raise RuntimeInvalidError(
                f"{self._root} is missing {', '.join(missing)}. {MATERIALIZE_HINT}"
            )
        return manifest

    def contains(self, path: Path) -> bool:
        """Whether an executable lives inside this runtime.

        Compared after `realpath`, which turns an 8.3 short name back into the
        long one and normalizes case, so the same file reached by two spellings
        is one file. This is half of the adoption rule (section 8.4) and half
        of the orphan rule (section 9.3).
        """
        root = os.path.normcase(os.path.realpath(self._root))
        candidate = os.path.normcase(os.path.realpath(path))
        return candidate.startswith(root.rstrip("\\/") + os.sep)

    def child_environment(
        self, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """The environment of `initdb`, `pg_ctl` and the server (section 5.3).

        `PATH` is built, not inherited: the runtime root, `Library\\bin`,
        `System32` and the Windows directory, in that order, and nothing from
        the user's `PATH` -- a PostgreSQL 15 there must not lend the server a
        `libssl`. Every `PG*` variable goes: `PGDATA`, `PGPORT` and
        `PGOPTIONS` in the user's environment would change which cluster
        `pg_ctl` controls, or the server's options. Everything else passes
        through, because Winsock itself needs `SystemRoot`.
        """
        source = dict(os.environ if base is None else base)
        system_root = next(
            (value for name, value in source.items() if name.upper() == "SYSTEMROOT"),
            r"C:\Windows",
        )
        environment = {
            name: value
            for name, value in source.items()
            if not name.upper().startswith("PG") and name.upper() != "PATH"
        }
        environment["PATH"] = os.pathsep.join(
            [
                str(self.command_root),
                str(self.bin_directory),
                str(Path(system_root) / "System32"),
                system_root,
            ]
        )
        return environment
