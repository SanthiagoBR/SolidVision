"""The runtime directory, and the environment its children get (RFC-033 5).

No binary runs here. A "runtime" is a directory with a manifest and four
files, so a handful of empty files is enough to ask the questions that
matter: is an arbitrary PostgreSQL installation refused, and does anything
from the user's environment reach `initdb`, `pg_ctl` or the server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.infrastructure.database.embedded.errors import RuntimeInvalidError
from app.infrastructure.database.embedded.runtime import (
    MANIFEST_NAME,
    REQUIRED_FILES,
    PostgresRuntime,
)

MANIFEST = {
    "format": 1,
    "postgresql": "16.15",
    "pgvector": "0.8.6",
    "lockfile_sha256": "0" * 64,
}


def make_runtime(root: Path, manifest: dict[str, object] | None = MANIFEST) -> Path:
    for relative in REQUIRED_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    if manifest is not None:
        (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_a_materialized_runtime_gives_its_versions(tmp_path: Path) -> None:
    manifest = PostgresRuntime(make_runtime(tmp_path / "postgres")).manifest()

    assert manifest.postgresql == "16.15"
    assert manifest.pgvector == "0.8.6"
    assert manifest.major == 16


def test_the_executables_are_absolute_paths_inside_the_runtime(tmp_path: Path) -> None:
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres"))

    for executable in (runtime.initdb, runtime.pg_ctl, runtime.postgres):
        assert executable.is_absolute()
        assert executable.parent == tmp_path / "postgres" / "Library" / "bin"


def test_a_relative_runtime_directory_is_refused() -> None:
    with pytest.raises(RuntimeInvalidError, match="absolute"):
        PostgresRuntime(Path("runtime/postgres"))


def test_a_directory_without_a_manifest_is_refused(tmp_path: Path) -> None:
    """Section 5.2: any other PostgreSQL installation fails explicitly.

    EnterpriseDB's `Program Files` layout has a `postgres.exe` too. What it
    does not have is the manifest, and without it the adapter would be
    running a version nobody measured.
    """
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres", manifest=None))

    with pytest.raises(RuntimeInvalidError, match="vendor_postgres.py"):
        runtime.manifest()


@pytest.mark.parametrize("missing", REQUIRED_FILES)
def test_a_missing_binary_is_named(tmp_path: Path, missing: str) -> None:
    root = make_runtime(tmp_path / "postgres")
    (root / missing).unlink()

    with pytest.raises(RuntimeInvalidError) as refusal:
        PostgresRuntime(root).manifest()

    assert missing in str(refusal.value)


@pytest.mark.parametrize(
    "manifest",
    [
        {**MANIFEST, "format": 2},
        {key: value for key, value in MANIFEST.items() if key != "pgvector"},
        {**MANIFEST, "postgresql": "sixteen"},
    ],
    ids=["another format", "a missing field", "a version that is not one"],
)
def test_a_manifest_that_cannot_be_trusted_is_refused(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres", manifest))

    with pytest.raises(RuntimeInvalidError):
        runtime.manifest()


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    root = make_runtime(tmp_path / "postgres")
    (root / MANIFEST_NAME).write_text("not json", encoding="utf-8")

    with pytest.raises(RuntimeInvalidError):
        PostgresRuntime(root).manifest()


USER_ENVIRONMENT = {
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": r"C:\Users\someone\AppData\Local\Temp",
    "PATH": r"C:\Program Files\PostgreSQL\15\bin;C:\tools",
    "PGDATA": r"C:\somewhere\else",
    "PGPORT": "5432",
    "PGOPTIONS": "-c fsync=off",
    "PGPASSWORD": "secret",
    "pgservice": "lower-case-too",
}


def test_no_pg_variable_reaches_the_children(tmp_path: Path) -> None:
    """Section 5.3: `PGDATA`, `PGPORT` and `PGOPTIONS` would change which
    cluster `pg_ctl` controls, or the server's options."""
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres"))

    environment = runtime.child_environment(USER_ENVIRONMENT)

    assert [name for name in environment if name.upper().startswith("PG")] == []


def test_the_path_is_built_and_holds_nothing_of_the_users(tmp_path: Path) -> None:
    """A PostgreSQL 15 on the user's `PATH` must not lend the server a DLL."""
    root = make_runtime(tmp_path / "postgres")

    environment = PostgresRuntime(root).child_environment(USER_ENVIRONMENT)

    assert environment["PATH"].split(os.pathsep) == [
        str(root),
        str(root / "Library" / "bin"),
        r"C:\Windows\System32",
        r"C:\Windows",
    ]


def test_a_path_spelt_in_another_case_is_replaced_not_duplicated(
    tmp_path: Path,
) -> None:
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres"))

    environment = runtime.child_environment(
        {"Path": r"C:\tools", "SystemRoot": "C:\\W"}
    )

    assert [name for name in environment if name.upper() == "PATH"] == ["PATH"]
    assert "C:\\tools" not in environment["PATH"]
    assert environment["PATH"].endswith("C:\\W")


def test_everything_else_passes_through(tmp_path: Path) -> None:
    """Winsock itself needs `SystemRoot`; an empty environment starts nothing."""
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres"))

    environment = runtime.child_environment(USER_ENVIRONMENT)

    assert environment["SYSTEMROOT"] == r"C:\Windows"
    assert environment["TEMP"] == USER_ENVIRONMENT["TEMP"]


def test_contains_tells_this_runtime_from_a_copy_beside_it(tmp_path: Path) -> None:
    """Half of the adoption rule and half of the orphan rule (8.4, 9.3).

    `postgres-copy` starts with the same characters as `postgres`; a prefix
    comparison without the separator would call it the same runtime.
    """
    runtime = PostgresRuntime(make_runtime(tmp_path / "postgres"))
    copy = make_runtime(tmp_path / "postgres-copy")

    assert runtime.contains(runtime.postgres)
    assert runtime.contains(Path(str(runtime.postgres).upper()))
    assert not runtime.contains(copy / "Library" / "bin" / "postgres.exe")
    assert not runtime.contains(
        Path(r"C:\Program Files\PostgreSQL\16\bin\postgres.exe")
    )
