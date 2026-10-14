"""One module knows where things are (RFC-034 sections 3, 4.1 and 10).

`app/infrastructure/config/layout.py` is the only module of `backend/app`
that may use `__file__`, the working directory, the home directory or
`LOCALAPPDATA`. Before RFC-034 four modules each climbed four directories
from their own file, and two defaults read the variable with two different
fallbacks; an anchor added to a fifth module would compile, pass every
behavioural test in a checkout, and break only in an installation.

Walked over the syntax tree, like the other architecture tests, so that a
comment or a docstring mentioning one of these is not a violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
LAYOUT = APP_DIR / "infrastructure" / "config" / "layout.py"

FORBIDDEN_NAMES = ("__file__",)
FORBIDDEN_CALLS = ("cwd", "getcwd", "home", "expanduser")
"""`Path.cwd()`, `os.getcwd()`, `Path.home()`, and `expanduser`, which is the
home directory by another spelling."""
FORBIDDEN_VARIABLES = ("LOCALAPPDATA",)


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py"))


def _anchors(path: Path) -> list[str]:
    """Every use of an anchor in one file, as `line: what`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CALLS:
            found.append(f"{node.lineno}: .{node.attr}")
        # A bare `getcwd()` needs its import, and the import is what is caught:
        # `home` and `cwd` are ordinary names for a variable.
        elif isinstance(node, ast.ImportFrom) and any(
            alias.name in FORBIDDEN_CALLS for alias in node.names
        ):
            found.append(f"{node.lineno}: an import of one of {FORBIDDEN_CALLS}")
        elif (
            isinstance(node, ast.Constant)
            and node.value in FORBIDDEN_VARIABLES
            and id(node) not in docstrings
        ):
            found.append(f"{node.lineno}: {node.value!r}")
    return found


def test_the_walk_finds_the_application() -> None:
    """Guards the guard: a wrong path would make the check below vacuous."""
    assert LAYOUT.is_file()
    assert len(_python_files(APP_DIR)) > 50


def test_the_walk_recognizes_every_anchor_it_forbids() -> None:
    """Guards the guard, on the one module allowed to have them all."""
    found = " | ".join(_anchors(LAYOUT))

    assert "__file__" in found
    assert ".home" in found
    assert "'LOCALAPPDATA'" in found


@pytest.mark.parametrize(
    ("source", "what"),
    [
        ("from pathlib import Path\nROOT = Path(__file__).parent\n", "__file__"),
        ("from pathlib import Path\nHERE = Path.cwd()\n", ".cwd"),
        ("import os\nHERE = os.getcwd()\n", ".getcwd"),
        ("from os import getcwd\nHERE = getcwd()\n", "getcwd"),
        ("from pathlib import Path\nHOME = Path.home()\n", ".home"),
        ("import os\nHOME = os.path.expanduser('~')\n", ".expanduser"),
        ("import os\nDATA = os.environ['LOCALAPPDATA']\n", "'LOCALAPPDATA'"),
        ("import os\nDATA = os.environ.get('LOCALAPPDATA')\n", "'LOCALAPPDATA'"),
    ],
)
def test_each_spelling_of_an_anchor_is_caught(
    tmp_path: Path, source: str, what: str
) -> None:
    module = tmp_path / "module.py"
    module.write_text(source, encoding="utf-8")

    assert any(what in anchor for anchor in _anchors(module))


def test_prose_about_an_anchor_is_not_an_anchor(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text(
        '"""LOCALAPPDATA"""\n\n# Path(__file__) and Path.cwd() and LOCALAPPDATA\n'
        'TEXT = "under %LOCALAPPDATA%, never Path.home()"\n',
        encoding="utf-8",
    )

    assert _anchors(module) == []


def test_no_module_of_the_application_but_layout_has_an_anchor() -> None:
    violations = {
        str(path.relative_to(BACKEND_DIR)): anchors
        for path in _python_files(APP_DIR)
        if path != LAYOUT and (anchors := _anchors(path))
    }

    assert violations == {}, (
        "Only app/infrastructure/config/layout.py may know where things are; "
        "take the path from `layout` or from `settings` instead."
    )
