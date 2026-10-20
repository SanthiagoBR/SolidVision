"""The OpenAPI document the frontend's types come from (RFC-037 section 4.3).

`frontend/src/api/openapi.json` is versioned, and this is the half of the drift
check that needs Python: the document the code produces, compared with the
file. The other half is in `npm test`, which compares `schema.d.ts` with what
`openapi-typescript` generates from the same file. Neither needs a server.

Both sides come from `scripts/export_openapi.py`, so that the title and the
version are the defaults of the `Settings` on both, and not whatever the
`.env` of whoever runs the suite says (section 11).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "export_openapi.py"


@pytest.fixture(scope="module")
def export() -> ModuleType:
    """The script, loaded from its file: `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_versioned_document_is_what_the_code_produces(export: ModuleType) -> None:
    document: Path = export.DOCUMENT
    assert (
        document.is_file()
    ), f"{document} is missing: run `npm run api:types` in frontend/"
    written: dict[str, Any] = json.loads(document.read_text(encoding="utf-8"))

    produced = export.openapi_document()

    assert sorted(produced["paths"]) == sorted(
        written["paths"]
    ), "The routes changed: run `npm run api:types` in frontend/"
    assert produced == written, (
        "The API changed and openapi.json did not: run `npm run api:types` in "
        "frontend/"
    )


def test_the_document_names_the_defaults_and_not_the_configuration(
    export: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a `PROJECT_NAME` and a `PROJECT_VERSION` in a `.env` would do."""
    from app.presentation.api import app

    monkeypatch.setattr(app, "title", "Somebody's SolidVision")
    monkeypatch.setattr(app, "version", "9.9.9")
    monkeypatch.setattr(app, "openapi_schema", None)

    produced = export.openapi_document()

    assert produced["info"]["title"] == "SolidVision"
    assert produced["info"]["version"] == "0.1.0"


def test_the_file_is_written_as_the_script_renders_it(export: ModuleType) -> None:
    """Keys sorted and a final newline: a regenerated file differs from the
    last only where the API did."""
    # `read_text` reads a CRLF as a LF, whatever a checkout with `core.autocrlf`
    # gave the file.
    text = export.DOCUMENT.read_text(encoding="utf-8")

    assert text == export.render(json.loads(text))
