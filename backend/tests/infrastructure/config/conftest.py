"""Layouts built by hand, in temporary directories (RFC-034 section 4.1).

Nothing here touches the checkout's `.env` or the user's real profile: a
`Layout` is a root and a data directory, and both are under `tmp_path`. That
is how the installed mode is exercised from inside a checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.config.layout import Layout, layout_at
from app.infrastructure.config.settings import RETIRED_KEYS, Settings


@pytest.fixture(autouse=True)
def no_setting_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """In development the environment is a source, so the tests start without
    the variables a developer's shell -- or `run -- pytest` -- may carry."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name in RETIRED_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def profile(tmp_path: Path) -> Path:
    """What `%LOCALAPPDATA%` would be."""
    directory = tmp_path / "profile" / "AppData" / "Local"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture()
def checkout(tmp_path: Path, profile: Path) -> Layout:
    """A repository: `pyproject.toml` at the root, and no `.env` yet."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return layout_at(root, profile)


@pytest.fixture()
def installation(tmp_path: Path, profile: Path) -> Layout:
    """An installed tree: no `pyproject.toml`. The data directory exists, as
    it does after a first run, and holds no `solidvision.env`."""
    root = tmp_path / "Program Files" / "SolidVision"
    root.mkdir(parents=True)
    where = layout_at(root, profile)
    where.data_directory.mkdir()
    return where
