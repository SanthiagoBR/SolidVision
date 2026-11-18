"""The license notices are read from what the package carries, never typed.

`scripts/third_party_notices.py` writes `THIRD-PARTY-NOTICES.md` from the
materialized runtimes and the interface's dependencies, and the staging puts
it in the installer beside the project's `LICENSE`. What is tested here is
the reading: which license a wheel declares, which of the interface's
packages are the runtime ones, what a runtime without its license texts
says. And, when the runtimes are there, that the file at the root is what
they give -- the drift test, as `openapi.json` has one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "third_party_notices.py"


@pytest.fixture(scope="module")
def notices() -> ModuleType:
    """The script, loaded from its file: `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("third_party_notices", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Reading a license
# --------------------------------------------------------------------------


def test_a_wheel_declares_its_license_by_expression_then_field_then_classifier(
    notices: ModuleType,
) -> None:
    declared = notices.declared_license

    assert declared({"License-Expression": "MIT", "License": "BSD"}, []) == "MIT"
    assert declared({"License": "BSD-3-Clause"}, ["MIT License"]) == "BSD-3-Clause"
    assert declared({}, ["MIT License", "BSD License"]) == "MIT License; BSD License"
    assert declared({}, []) == "não declarada"


def test_a_whole_license_text_in_the_field_is_not_taken_for_its_name(
    notices: ModuleType,
) -> None:
    """Some wheels put the full text in `License`; the classifier names it."""
    text = "Copyright (c) 2010\n\nPermission is hereby granted, free of charge"

    assert notices.declared_license({"License": text}, ["MIT License"]) == (
        "MIT License"
    )


def test_a_license_text_with_backticks_stays_inside_its_fence(
    notices: ModuleType,
) -> None:
    fenced = notices.fenced("before\n```\nafter")

    assert fenced.startswith("````text\n")
    assert fenced.endswith("\n````")


# --------------------------------------------------------------------------
# What is read, and from where
# --------------------------------------------------------------------------


def test_the_interface_packages_are_the_lockfiles_not_for_development(
    notices: ModuleType, tmp_path: Path
) -> None:
    """The build bundles the runtime packages and drops their license files,
    so their texts are the ones the notices carry in full."""
    frontend = tmp_path / "frontend"
    lock = {
        "packages": {
            "": {"name": "solidvision"},
            "node_modules/kept": {"version": "1.2.3", "license": "MIT"},
            "node_modules/tool": {"version": "9.9.9", "license": "MIT", "dev": True},
        }
    }
    write(frontend / "package-lock.json", json.dumps(lock))
    write(frontend / "node_modules" / "kept" / "LICENSE", "MIT License\n\nkept")
    write(
        frontend / "node_modules" / "kept" / "package.json",
        json.dumps({"repository": {"url": "https://example.org/kept"}}),
    )

    found = notices.frontend_packages(tmp_path)

    assert [(component.name, component.version) for component, _ in found] == [
        ("kept", "1.2.3")
    ]
    component, text = found[0]
    assert component.license == "MIT"
    assert component.source == "https://example.org/kept"
    assert text == "MIT License\n\nkept"


def test_a_runtime_package_without_a_license_file_is_named(
    notices: ModuleType, tmp_path: Path
) -> None:
    frontend = tmp_path / "frontend"
    lock = {"packages": {"node_modules/bare": {"version": "1.0.0", "license": "MIT"}}}
    write(frontend / "package-lock.json", json.dumps(lock))
    write(frontend / "node_modules" / "bare" / "package.json", "{}")

    with pytest.raises(notices.NoticesError, match="bare"):
        notices.frontend_packages(tmp_path)


def test_a_postgres_runtime_from_before_the_license_texts_says_to_vendor_again(
    notices: ModuleType, tmp_path: Path
) -> None:
    write(
        tmp_path / "runtime" / "postgres" / "conda-meta" / "zlib-1.0-h0.json",
        json.dumps({"name": "zlib", "version": "1.0", "build": "h0"}),
    )

    with pytest.raises(notices.NoticesError, match="vendor_postgres.py"):
        notices.postgres_packages(tmp_path)


def test_each_postgres_package_lists_the_texts_its_archive_carried(
    notices: ModuleType, tmp_path: Path
) -> None:
    postgres = tmp_path / "runtime" / "postgres"
    for name, license_, texts in (
        ("zlib", "Zlib", ["LICENSE"]),
        ("vc", "BSD-3-Clause", []),
    ):
        write(
            postgres / "conda-meta" / f"{name}-1.0-h0.json",
            json.dumps(
                {"name": name, "version": "1.0", "build": "h0", "license": license_}
            ),
        )
        for text in texts:
            write(postgres / "licenses" / f"{name}-1.0-h0" / text, "the text")

    found = {
        component.name: component for component in notices.postgres_packages(tmp_path)
    }

    assert found["zlib"].texts == ("zlib-1.0-h0/LICENSE",)
    assert found["zlib"].license == "Zlib"
    assert found["vc"].texts == ()


# --------------------------------------------------------------------------
# The file at the root
# --------------------------------------------------------------------------


def test_the_notices_at_the_root_are_what_the_runtimes_give(
    notices: ModuleType,
) -> None:
    """The drift test. Without the runtimes it cannot say anything, and is
    skipped with the commands that materialize them; `npm run dist` runs the
    same comparison before it stages the package."""
    needed = {
        "runtime/python/Lib/site-packages": "python scripts/vendor_python.py",
        "runtime/postgres/licenses": "python scripts/vendor_postgres.py",
        "runtime/models": "python scripts/vendor_models.py",
        "frontend/node_modules": "npm ci, in frontend/",
    }
    missing = [
        command for path, command in needed.items() if not (REPOSITORY / path).is_dir()
    ]
    if missing:
        pytest.skip("the runtimes are not materialized: " + "; ".join(missing))

    expected = notices.render(REPOSITORY)
    current = (REPOSITORY / notices.OUTPUT_NAME).read_text(encoding="utf-8")

    assert notices.normalized(current) == notices.normalized(
        expected
    ), "THIRD-PARTY-NOTICES.md is stale: run `python scripts/third_party_notices.py`"
