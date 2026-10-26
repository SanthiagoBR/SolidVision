"""One version, written in three places (RFC-041 section 2.11).

`frontend/package.json` names the installer and goes into the User-Agent of
the tiles (RFC-040 section 5.6); the default of `project_version` goes into
the User-Agent of the address search (RFC-043 section 8.3) and the OpenAPI
document; `pyproject.toml` is the checkout's. Nothing else keeps them equal,
and `package.json` is the installer's source.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from app.infrastructure.config.settings import Settings

REPO = Path(__file__).resolve().parents[2]


def installer_version() -> str:
    package = json.loads(
        (REPO / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    version = package["version"]
    assert isinstance(version, str)
    return version


def test_the_apis_version_is_the_installers() -> None:
    assert Settings.model_fields["project_version"].default == installer_version()


def test_the_checkouts_version_is_the_installers() -> None:
    with (REPO / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["version"] == installer_version()
