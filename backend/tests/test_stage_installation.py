"""The staged installation is an installation, and never a checkout (RFC-034 9).

`scripts/stage_installation.py` is what makes "the backend starts without a
checkout" testable, so what it copies is itself tested: the tree of section
4.1, without the two files that would turn the copy back into a checkout. An
installer that carried `pyproject.toml` would ship an app that takes itself
for development (section 13), and this is the guard the RFC names for it.

The source here is a miniature repository in a temporary directory. The real
one is staged by the measurement of section 16, with the 123 MB runtime.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.infrastructure.config.layout import Environment, layout_at

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stage_installation.py"

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="The lock is a Windows DACL, written by icacls"
)


@pytest.fixture(scope="module")
def staging() -> ModuleType:
    """The script, loaded from its file: `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("stage_installation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A checkout in miniature, with everything a staging must leave behind."""
    root = tmp_path / "repository"
    files = {
        "pyproject.toml": "[project]\n",
        ".env": "DEBUG=true\n",
        "backend/alembic.ini": "[alembic]\n",
        "backend/alembic/env.py": "REVISIONS = 'here'\n",
        "backend/alembic/versions/0001_first.py": "revision = '0001'\n",
        "backend/app/__init__.py": "",
        "backend/app/module.py": "VALUE = 1\n",
        "backend/app/package/__init__.py": "",
        "backend/app/package/deep.py": "VALUE = 2\n",
        "backend/app/__pycache__/module.cpython-39.pyc": "stale",
        "backend/tests/test_module.py": "def test_nothing() -> None: ...\n",
        "backend/requirements.txt": "pytest\n",
        "runtime/postgres/SOLIDVISION-RUNTIME.json": "{}\n",
        "runtime/postgres/Library/bin/postgres.exe": "not really\n",
        "docs/README.md": "# docs\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def entries(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def compiled(root: Path) -> set[str]:
    return {path.name.split(".")[0] for path in root.rglob("__pycache__/*.pyc")}


# --------------------------------------------------------------------------
# What is copied, and what never is
# --------------------------------------------------------------------------


def test_the_staging_is_the_installed_tree_and_nothing_else(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "Program Files" / "SolidVision"

    staged = staging.stage(target, source=repository)

    assert staged.root == target
    assert entries(target) == {
        staging.MARKER_NAME,
        "backend/alembic.ini",
        "backend/alembic/env.py",
        "backend/alembic/versions/0001_first.py",
        "backend/app/__init__.py",
        "backend/app/module.py",
        "backend/app/package/__init__.py",
        "backend/app/package/deep.py",
        "runtime/postgres/SOLIDVISION-RUNTIME.json",
        "runtime/postgres/Library/bin/postgres.exe",
    }


def test_the_copy_is_an_installation_and_the_source_is_a_checkout(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """The mode is read from the tree (section 4.2), so this is the whole
    point of staging: the same code, and the other mode."""
    profile = tmp_path / "profile"
    target = tmp_path / "staged"

    staging.stage(target, source=repository)

    assert layout_at(repository, profile).environment is Environment.DEVELOPMENT
    assert layout_at(target, profile).environment is Environment.INSTALLED
    assert not (target / "pyproject.toml").exists()
    assert not (target / ".env").exists()
    assert not list(target.rglob("test_*.py"))


def test_a_tree_that_would_stage_tests_is_refused_and_nothing_is_left(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    (repository / "backend" / "app" / "tests").mkdir()
    target = tmp_path / "staged"

    with pytest.raises(staging.StagingError, match="never staged"):
        staging.stage(target, source=repository)

    assert not target.exists()


def test_a_source_without_the_runtime_says_how_to_get_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    runtime = repository / "runtime"
    for path in sorted(runtime.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    runtime.rmdir()
    target = tmp_path / "staged"

    with pytest.raises(staging.StagingError, match="vendor_postgres.py"):
        staging.stage(target, source=repository)

    assert not target.exists()


# --------------------------------------------------------------------------
# The bytecode (section 9's hypothesis is measured with both)
# --------------------------------------------------------------------------


def test_the_copy_is_compiled_for_this_interpreter(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"

    staged = staging.stage(target, source=repository)

    assert staged.compiled is True
    assert {"module", "deep", "env", "0001_first"} <= compiled(target)
    tags = {path.name.split(".")[-2] for path in target.rglob("__pycache__/*.pyc")}
    assert tags == {sys.implementation.cache_tag}, "the source's stale one stayed"


def test_no_compile_leaves_no_bytecode_at_all(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"

    staged = staging.stage(target, source=repository, compile_bytecode=False)

    assert staged.compiled is False
    assert compiled(target) == set()
    assert not list(target.rglob("__pycache__"))


def test_a_staging_read_back_says_what_was_done_to_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"
    staging.stage(target, source=repository, compile_bytecode=False)
    assert staging.staged_at(target).compiled is False

    staging.compile_tree(target)

    assert staging.staged_at(target).compiled is True
    assert staging.staged_at(target).read_only is False


# --------------------------------------------------------------------------
# Where a staging may be
# --------------------------------------------------------------------------


def test_a_target_inside_the_repository_is_refused(
    staging: ModuleType, repository: Path
) -> None:
    with pytest.raises(staging.StagingError, match="outside the repository"):
        staging.stage(repository / "staged", source=repository)

    assert not (repository / "staged").exists()


def test_a_relative_target_is_refused(staging: ModuleType, repository: Path) -> None:
    with pytest.raises(staging.StagingError, match="must be absolute"):
        staging.stage(Path("staged"), source=repository)


def test_an_existing_directory_is_refused_and_left_as_it_was(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "somebody elses"
    target.mkdir()
    (target / "theirs.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(staging.StagingError, match="left alone"):
        staging.stage(target, source=repository)

    assert entries(target) == {"theirs.txt"}


def test_an_existing_staging_is_refused_with_the_way_to_remove_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"
    staging.stage(target, source=repository)

    with pytest.raises(staging.StagingError, match="--remove"):
        staging.stage(target, source=repository)


@pytest.mark.parametrize("action", ["lock", "unlock", "remove", "compile_tree"])
def test_a_directory_that_is_not_a_staging_is_never_touched(
    staging: ModuleType, tmp_path: Path, action: str
) -> None:
    """`--remove` deletes a tree and `--unlock` rewrites its DACL: neither may
    be pointed at a directory this script did not make."""
    directory = tmp_path / "photos"
    directory.mkdir()
    (directory / "one.jpg").write_text("a photograph", encoding="utf-8")

    with pytest.raises(staging.StagingError, match="not a staging"):
        getattr(staging, action)(directory)

    assert entries(directory) == {"one.jpg"}


# --------------------------------------------------------------------------
# Running the copy, and the lock
# --------------------------------------------------------------------------


def test_the_environment_of_a_staged_process_names_the_copy_and_not_the_checkout(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"
    profile = tmp_path / "profile"
    staged = staging.stage(target, source=repository)

    environment = staged.environment(
        profile, base={"PYTHONPATH": "backend", "SYSTEMROOT": r"C:\Windows"}
    )

    assert environment["PYTHONPATH"] == str(target / "backend")
    assert environment["LOCALAPPDATA"] == str(profile)
    assert environment["SYSTEMROOT"] == r"C:\Windows"


def writable(directory: Path) -> bool:
    probe = directory / "written-by-the-test"
    try:
        probe.write_text("x", encoding="utf-8")
    except PermissionError:
        return False
    probe.unlink()
    return True


@windows_only
def test_a_locked_staging_cannot_be_written_and_can_be_given_back(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """What `C:\\Program Files` is to an ordinary process, by the real
    `icacls.exe`, and undone by the same user who cannot write to it."""
    target = tmp_path / "Program Files" / "SolidVision"
    staged = staging.stage(target, source=repository, read_only=True)
    try:
        assert staged.read_only is True
        assert not writable(target)
        assert not writable(target / "backend" / "app" / "package")
        assert not writable(target / "runtime" / "postgres")
        with pytest.raises(PermissionError):
            (target / "backend" / "app" / "module.py").write_text("VALUE = 3\n")
        with pytest.raises(PermissionError):
            os.remove(target / "backend" / "app" / "module.py")
        assert (target / "backend" / "app" / "module.py").read_text() == "VALUE = 1\n"
    finally:
        staging.unlock(target)

    assert staging.staged_at(target).read_only is False
    assert writable(target / "backend" / "app" / "package")

    staging.remove(target)

    assert not target.exists()
