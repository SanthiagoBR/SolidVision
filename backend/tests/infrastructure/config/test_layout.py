"""The mode, the root and the data directory (RFC-034 sections 4 and 6.2)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.infrastructure.config.layout import (
    Environment,
    Layout,
    layout,
    layout_at,
    local_app_data,
)

BACKEND = Path(__file__).resolve().parents[3]

CSIDL_LOCAL_APPDATA = 0x001C


def asked_of_windows() -> Path:
    """The per-user application data folder, by an older API than the one
    `layout.py` calls (`SHGetFolderPathW`, not `SHGetKnownFolderPath`).

    Not `os.environ["LOCALAPPDATA"]`: pointing that variable at a temporary
    directory is how a validation run keeps a process off the real profile
    (section 6.2), and a test that took it for Windows' answer would fail in
    exactly that run.
    """
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    shell32: Any = ctypes.WinDLL("shell32")
    result = shell32.SHGetFolderPathW(None, CSIDL_LOCAL_APPDATA, None, 0, buffer)
    assert result == 0, f"SHGetFolderPathW returned {result:#x}"
    return Path(buffer.value)


# --------------------------------------------------------------------------
# The mode is read, not declared (section 4.2)
# --------------------------------------------------------------------------


def test_a_root_with_pyproject_toml_is_a_checkout(
    tmp_path: Path, profile: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    assert layout_at(tmp_path, profile).environment is Environment.DEVELOPMENT


def test_a_root_without_it_is_an_installation(tmp_path: Path, profile: Path) -> None:
    assert layout_at(tmp_path, profile).environment is Environment.INSTALLED


def test_git_alone_does_not_make_a_checkout(tmp_path: Path, profile: Path) -> None:
    """An exported checkout has no `.git`, so `.git` cannot be the proof."""
    (tmp_path / ".git").mkdir()

    assert layout_at(tmp_path, profile).environment is Environment.INSTALLED


def test_an_env_file_alone_does_not_make_a_checkout(
    tmp_path: Path, profile: Path
) -> None:
    """Development needs proof, and a `.env` left in an installation is not it."""
    (tmp_path / ".env").write_text("DEBUG=true\n", encoding="utf-8")

    assert layout_at(tmp_path, profile).environment is Environment.INSTALLED


def test_no_variable_changes_the_mode(
    tmp_path: Path, profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A variable for it would be the second place the roadmap asks not to
    exist, and a declaration again."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SOLIDVISION_ENVIRONMENT", "development")

    assert layout_at(tmp_path, profile).environment is Environment.INSTALLED


def test_the_running_code_is_a_checkout() -> None:
    """Under `pytest` this is the repository, found from `layout.py` itself."""
    assert layout.environment is Environment.DEVELOPMENT
    assert (layout.root / "pyproject.toml").is_file()
    assert layout.backend_directory == BACKEND
    assert (layout.backend_directory / "alembic.ini").is_file()


# --------------------------------------------------------------------------
# The data directory (section 6.2)
# --------------------------------------------------------------------------


def test_development_and_installed_are_sibling_directories(
    checkout: Layout, installation: Layout, profile: Path
) -> None:
    """A sibling, not a folder inside: whatever an uninstaller does to the
    user's data must not reach a developer's (section 2.5)."""
    assert installation.data_directory == profile / "SolidVision"
    assert checkout.data_directory == profile / "SolidVision-dev"
    assert installation.data_directory not in checkout.data_directory.parents


def test_the_variable_is_obeyed_when_it_is_absolute(tmp_path: Path) -> None:
    assert local_app_data({"LOCALAPPDATA": str(tmp_path)}) == tmp_path


@pytest.mark.skipif(sys.platform != "win32", reason="Asks Windows for the folder")
@pytest.mark.parametrize("environ", [{}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "x"}])
def test_without_a_usable_variable_windows_is_asked(environ: dict[str, str]) -> None:
    """One answer, the right one, instead of each default guessing a
    different fallback (measured in section 2.2). A relative value is not a
    place: it would move with the working directory."""
    assert os.path.samefile(local_app_data(environ), asked_of_windows())


def test_a_layout_is_absolute_or_it_is_not_built(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root"):
        Layout(Environment.INSTALLED, Path("relative"), tmp_path)
    with pytest.raises(ValueError, match="data_directory"):
        Layout(Environment.INSTALLED, tmp_path, Path("relative"))


# --------------------------------------------------------------------------
# What is derived (sections 4.1 and 6.1)
# --------------------------------------------------------------------------


def test_each_mode_reads_one_file_and_never_the_others(
    checkout: Layout, installation: Layout
) -> None:
    assert checkout.config_file == checkout.root / ".env"
    assert installation.config_file == installation.data_directory / "solidvision.env"


def test_a_relative_path_is_relative_to_the_folder_of_the_file(
    checkout: Layout, installation: Layout
) -> None:
    assert checkout.config_base == checkout.root
    assert installation.config_base == installation.data_directory


@pytest.mark.parametrize("mode", ["checkout", "installation"])
def test_the_tree_is_the_same_in_both_modes(
    mode: str, request: pytest.FixtureRequest
) -> None:
    where: Layout = request.getfixturevalue(mode)

    assert where.backend_directory == where.root / "backend"
    assert where.postgres_runtime == where.root / "runtime" / "postgres"
    assert where.log_directory == where.data_directory / "logs"
    assert where.thumbnail_directory == where.data_directory / "thumbnails"
    assert where.postgres_directory == where.data_directory / "postgres"


def test_building_a_layout_creates_nothing(tmp_path: Path, profile: Path) -> None:
    root = tmp_path / "nowhere"

    where = layout_at(root, profile)

    assert not root.exists()
    assert not where.data_directory.exists()


# --------------------------------------------------------------------------
# Real processes: the answer depends on neither the working directory nor
# on the variable being there (sections 6.2 and 10)
# --------------------------------------------------------------------------

PRINT_THE_LAYOUT = """
import json
from app.infrastructure.config.layout import layout
print(json.dumps({
    "environment": layout.environment.value,
    "root": str(layout.root),
    "data": str(layout.data_directory),
    "config": str(layout.config_file),
}))
"""


def layout_seen_from(cwd: Path, environment: dict[str, str]) -> dict[str, str]:
    completed = subprocess.run(
        [sys.executable, "-c", PRINT_THE_LAYOUT],
        cwd=cwd,
        env={**environment, "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    seen: dict[str, str] = json.loads(completed.stdout)
    return seen


def test_two_working_directories_see_the_same_layout(tmp_path: Path) -> None:
    elsewhere = tmp_path / "somewhere else"
    elsewhere.mkdir()

    here = layout_seen_from(BACKEND, dict(os.environ))
    there = layout_seen_from(elsewhere, dict(os.environ))

    assert here == there
    assert here["root"] == str(layout.root)


@pytest.mark.skipif(sys.platform != "win32", reason="Asks Windows for the folder")
def test_a_process_without_localappdata_finds_the_same_data_directory(
    tmp_path: Path,
) -> None:
    """A launcher may build its children's environment from nothing. Before
    RFC-034 the thumbnails and the cluster then went to two different roots
    under the home directory (section 2.2)."""
    bare = {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "LOCALAPPDATA"
    }

    without_it = layout_seen_from(tmp_path, bare)

    assert os.path.samefile(Path(without_it["data"]).parent, asked_of_windows())
    assert Path(without_it["data"]).name == "SolidVision-dev"
