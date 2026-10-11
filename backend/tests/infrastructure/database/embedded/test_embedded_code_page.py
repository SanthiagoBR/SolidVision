"""Paths outside the ANSI code page (RFC-033 section 10).

The second branch of section 10, the one the measurement put in force: a
path the code page cannot carry is replaced by its 8.3 short form, and a
volume with no short form to offer gets a refusal that says what to change.

The name used here mixes Polish and Japanese because no single-byte or
double-byte ANSI code page holds both; the tests are skipped on a Windows set
to UTF-8 (code page 65001), where every path fits and there is nothing to
decide.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pytest

from app.infrastructure.database.embedded import code_page
from app.infrastructure.database.embedded.cluster_home import ClusterHome, DirectoryAcl
from app.infrastructure.database.embedded.code_page import (
    argv_safe,
    fits_ansi_code_page,
    short_path,
)
from app.infrastructure.database.embedded.errors import EmbeddedPostgresError
from app.infrastructure.database.embedded.runtime import (
    MANIFEST_NAME,
    REQUIRED_FILES,
    PostgresRuntime,
)

OUTSIDE = "Łukasz 東京"


def _ansi_code_page() -> int:
    return int(ctypes.WinDLL("kernel32").GetACP()) if sys.platform == "win32" else 0


pytestmark = [
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
    ),
    pytest.mark.skipif(
        _ansi_code_page() == 65001,
        reason="With a UTF-8 code page every path fits; nothing to decide",
    ),
]


class NoAcl(DirectoryAcl):
    def restrict_to_current_user(self, directory: Path) -> None:
        return None


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    directory = tmp_path / OUTSIDE
    directory.mkdir()
    return directory


def volume_has_short_names(directory: Path) -> bool:
    return short_path(directory).isascii()


def test_ascii_fits_and_a_name_in_two_scripts_does_not() -> None:
    assert fits_ansi_code_page(r"C:\Users\someone\AppData\Local") is True
    assert fits_ansi_code_page(OUTSIDE) is False


def test_a_best_fit_look_alike_is_not_called_a_fit() -> None:
    """`Ł` has a best-fit `L` in code page 1252. A path "converted" that way
    arrives intact and names another directory, which is worse than `?`."""
    if _ansi_code_page() != 1252:
        pytest.skip("the look-alike is specific to code page 1252")

    assert fits_ansi_code_page("Łukasz") is False
    assert fits_ansi_code_page("João Silva") is True


def test_a_path_that_fits_is_handed_over_untouched(tmp_path: Path) -> None:
    plain = tmp_path / "Solid Vision"
    plain.mkdir()

    assert argv_safe(plain, setting="ANY") == plain


def test_a_path_that_does_not_fit_becomes_ascii_and_the_same_directory(
    outside: Path,
) -> None:
    if not volume_has_short_names(outside):
        pytest.skip("this volume generates no 8.3 short names")

    safe = argv_safe(outside, setting="EMBEDDED_POSTGRES_DIRECTORY")

    assert str(safe).isascii()
    assert os.path.samefile(safe, outside)


def test_without_a_short_name_the_refusal_names_the_setting(
    outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A volume with short names turned off answers with the long name."""
    monkeypatch.setattr(code_page, "short_path", lambda path: str(path))

    with pytest.raises(EmbeddedPostgresError) as refusal:
        argv_safe(outside, setting="EMBEDDED_POSTGRES_DIRECTORY")

    assert "EMBEDDED_POSTGRES_DIRECTORY" in str(refusal.value)
    assert "code page" in str(refusal.value)


def test_the_cluster_files_are_respelt_only_once_the_directory_exists(
    outside: Path,
) -> None:
    if not volume_has_short_names(outside):
        pytest.skip("this volume generates no 8.3 short names")
    home = ClusterHome(outside / "SolidVision" / "postgres", acl=NoAcl())
    files = home.files(16)

    assert home.for_commands(files) == files, "nothing to run where nothing is"

    home.ensure_directory(files)
    respelt = home.for_commands(files)

    assert str(respelt.data).isascii()
    assert os.path.samefile(respelt.directory, files.directory)
    assert respelt.data.name == "data"
    assert respelt.password.name == "password"


def test_the_runtime_is_started_through_a_path_that_fits(outside: Path) -> None:
    """`initdb` reads its own location in the code page to find `postgres.exe`."""
    if not volume_has_short_names(outside):
        pytest.skip("this volume generates no 8.3 short names")
    root = outside / "postgres"
    for relative in REQUIRED_FILES:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"")
    (root / MANIFEST_NAME).write_text(
        '{"format": 1, "postgresql": "16.15", "pgvector": "0.8.6", '
        '"lockfile_sha256": "0"}',
        encoding="utf-8",
    )
    runtime = PostgresRuntime(root)

    assert runtime.manifest().major == 16
    assert runtime.root == root, "the root that is shown stays the one given"
    for executable in (runtime.initdb, runtime.pg_ctl, runtime.postgres):
        assert str(executable).isascii()
        assert executable.is_file()
    assert all(
        entry.isascii() for entry in runtime.child_environment({})["PATH"].split(";")
    )
    assert runtime.contains(runtime.postgres)
    assert runtime.contains(root / "Library" / "bin" / "postgres.exe")
