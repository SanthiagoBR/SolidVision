"""The staged installation is an installation, and never a checkout (RFC-034 9).

`scripts/stage_installation.py` is what makes "the backend starts without a
checkout" testable, so what it copies is itself tested: the tree of section
4.1, without the two files that would turn the copy back into a checkout. An
installer that carried `pyproject.toml` would ship an app that takes itself
for development (section 13), and this is the guard the RFC names for it.

The source here is a miniature repository in a temporary directory. The real
one is staged by the measurement of section 16, with the 123 MB runtime, and
since RFC-041 by `npm run dist`, as the installer's `resources` folder: with
the embedded Python and the interface, compiled by that Python in
`unchecked-hash` mode (RFC-041 sections 5.4 and 7.2).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from app.infrastructure.config.layout import Environment, layout_at

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stage_installation.py"

INTERPRETER = Path(sys.executable)
"""What compiles the miniature's bytecode: its `runtime\\python\\python.exe` is
a text file. The real staging compiles with the copy's own interpreter."""

UNCHECKED_HASH = 0b01
"""The flags of a `.pyc` header (PEP 552): hash-based, source not checked."""

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
        "runtime/models/SOLIDVISION-MODELS.json": "{}\n",
        "runtime/models/clip/model.safetensors": "not really either\n",
        "runtime/python/python.exe": "not an interpreter\n",
        "runtime/python/python312._pth": "python312.zip\n",
        "runtime/python/SOLIDVISION-PYTHON.json": "{}\n",
        "runtime/python/Lib/site-packages/six.py": "VALUE = 6\n",
        "runtime/python/Lib/site-packages/__pycache__/six.cpython-312.pyc": "its own",
        "frontend/dist/index.html": "<!doctype html>\n",
        "frontend/dist/assets/index.js": "export {};\n",
        "frontend/src/main.tsx": "export {};\n",
        "frontend/package.json": "{}\n",
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

    staged = staging.stage(target, source=repository, python=INTERPRETER)

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
        "runtime/models/SOLIDVISION-MODELS.json",
        "runtime/models/clip/model.safetensors",
        "runtime/python/python.exe",
        "runtime/python/python312._pth",
        "runtime/python/SOLIDVISION-PYTHON.json",
        "runtime/python/Lib/site-packages/six.py",
        "frontend/dist/index.html",
        "frontend/dist/assets/index.js",
    }


def test_the_interpreter_keeps_its_own_bytecode_and_the_backend_loses_the_checkouts(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """`vendor_python.py` compiled `site-packages` with the very interpreter
    that reads it; a checkout's `__pycache__` is of whatever ran it last."""
    target = tmp_path / "staged"

    staging.stage(target, source=repository, python=INTERPRETER)

    kept = target / "runtime/python/Lib/site-packages/__pycache__/six.cpython-312.pyc"
    assert kept.read_text(encoding="utf-8") == "its own"
    assert not (target / "backend/app/__pycache__/module.cpython-39.pyc").exists()


def test_the_backend_is_compiled_in_unchecked_hash_mode(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """RFC-041 section 5.4: a `.pyc` that is not compared with its source's
    date, so an extraction that does not keep dates changes nothing."""
    target = tmp_path / "staged"

    staging.stage(target, source=repository, python=INTERPRETER)

    caches = sorted((target / "backend").rglob("__pycache__/*.pyc"))
    assert caches
    for cache in caches:
        flags = int.from_bytes(cache.read_bytes()[4:8], "little")
        assert flags == UNCHECKED_HASH, cache


def test_the_copy_runs_on_its_own_python_without_pythonpath(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"

    staged = staging.stage(target, source=repository, python=INTERPRETER)

    assert staged.python == target / "runtime" / "python" / "python.exe"
    how = staging._how_to_run(staged)
    assert f'& "{staged.python}" -m app.launcher' in how
    assert "PYTHONPATH" not in how


def test_a_licenses_tree_over_the_path_budget_is_zipped_in_the_copy(
    staging: ModuleType,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC-041 section 2.5. The budget is made tight by a longer prefix, so
    the files on disk stay short whatever this machine allows."""
    check_package = importlib.import_module("check_package")

    licenses = (
        repository
        / "runtime/python/Lib/site-packages/torch-2.13.0+cpu.dist-info"
        / "licenses"
    )
    (licenses / "third_party").mkdir(parents=True)
    (licenses / "third_party" / "LICENSE.txt").write_text("notice", encoding="utf-8")
    monkeypatch.setattr(check_package, "RESOURCES_PREFIX", "C:\\" + "p" * 180)
    target = tmp_path / "staged"

    staging.stage(target, source=repository, python=INTERPRETER)

    dist_info = target / "runtime/python/Lib/site-packages/torch-2.13.0+cpu.dist-info"
    assert not (dist_info / "licenses").exists()
    with zipfile.ZipFile(dist_info / "licenses.zip") as archive:
        assert archive.namelist() == ["third_party/LICENSE.txt"]
    assert (licenses / "third_party" / "LICENSE.txt").is_file(), "the source is left"


def test_a_source_without_the_interface_says_how_to_build_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    for path in sorted((repository / "frontend" / "dist").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (repository / "frontend" / "dist").rmdir()

    with pytest.raises(staging.StagingError, match="npm run build"):
        staging.stage(tmp_path / "staged", source=repository, python=INTERPRETER)


def test_a_source_without_the_python_runtime_says_how_to_get_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    python = repository / "runtime" / "python"
    for path in sorted(python.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    python.rmdir()

    with pytest.raises(staging.StagingError, match="runtime/python.*vendor_python"):
        staging.stage(tmp_path / "staged", source=repository, python=INTERPRETER)


def test_the_copy_is_an_installation_and_the_source_is_a_checkout(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """The mode is read from the tree (section 4.2), so this is the whole
    point of staging: the same code, and the other mode."""
    profile = tmp_path / "profile"
    target = tmp_path / "staged"

    staging.stage(target, source=repository, python=INTERPRETER)

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
        staging.stage(target, source=repository, python=INTERPRETER)

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
        staging.stage(target, source=repository, python=INTERPRETER)

    assert not target.exists()


def test_a_source_without_the_models_says_how_to_get_them(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """RFC-035 section 14: an installation carries the models beside the code."""
    models = repository / "runtime" / "models"
    for path in sorted(models.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    models.rmdir()
    target = tmp_path / "staged"

    with pytest.raises(staging.StagingError, match="runtime/models.*vendor_models"):
        staging.stage(target, source=repository, python=INTERPRETER)

    assert not target.exists()


# --------------------------------------------------------------------------
# The bytecode (section 9's hypothesis is measured with both)
# --------------------------------------------------------------------------


def test_the_copy_is_compiled_for_this_interpreter(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"

    staged = staging.stage(target, source=repository, python=INTERPRETER)

    backend = target / "backend"
    assert staged.compiled is True
    assert compiled(backend) == {"module", "deep", "__init__", "env", "0001_first"}
    tags = {path.name.split(".")[-2] for path in backend.rglob("__pycache__/*.pyc")}
    assert tags == {sys.implementation.cache_tag}, "the source's stale one stayed"


def test_no_compile_leaves_no_bytecode_in_the_backend(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """The interpreter's own bytecode stays: it is part of the runtime."""
    target = tmp_path / "staged"

    staged = staging.stage(
        target, source=repository, python=INTERPRETER, compile_bytecode=False
    )

    assert staged.compiled is False
    assert compiled(target / "backend") == set()
    assert not list((target / "backend").rglob("__pycache__"))
    assert compiled(target / "runtime" / "python") == {"six"}


def test_a_staging_read_back_says_what_was_done_to_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"
    staging.stage(target, source=repository, python=INTERPRETER, compile_bytecode=False)
    assert staging.staged_at(target).compiled is False

    staging.compile_tree(target, python=INTERPRETER)

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
        staging.stage(target, source=repository, python=INTERPRETER)

    assert entries(target) == {"theirs.txt"}


def test_an_existing_staging_is_refused_with_the_way_to_remove_it(
    staging: ModuleType, repository: Path, tmp_path: Path
) -> None:
    target = tmp_path / "staged"
    staging.stage(target, source=repository, python=INTERPRETER)

    with pytest.raises(staging.StagingError, match="--remove"):
        staging.stage(target, source=repository, python=INTERPRETER)


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
    staged = staging.stage(target, source=repository, python=INTERPRETER)

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
    staged = staging.stage(
        target, source=repository, python=INTERPRETER, read_only=True
    )
    try:
        assert staged.read_only is True
        assert not writable(target)
        assert not writable(target / "backend" / "app" / "package")
        assert not writable(target / "runtime" / "postgres")
        assert not writable(target / "runtime" / "models")
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
