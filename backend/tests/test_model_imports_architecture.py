"""The model libraries are imported only inside the package that prepares them.

RFC-035 section 5.2 turns the offline mode on in the `__init__` of
`app.infrastructure.ai`, because `huggingface_hub` reads it once, when it is
first loaded, and Python runs a package's `__init__` before any of its
submodules. That only holds while no module outside the package imports one
of these libraries itself: one `import transformers` in a router, a worker or
a CLI, reached before the package, and the process would load its models with
the network on. Section 9 names this test as what keeps it so.

Walked over the syntax tree, like the other architecture tests, so that a
comment or a docstring naming a library is not a violation. Function-local
imports count: they run before the package just the same.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
AI_DIR = APP_DIR / "infrastructure" / "ai"

MODEL_LIBRARIES = ("torch", "transformers", "huggingface_hub", "safetensors")


def _python_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_walk_finds_the_libraries_where_they_belong() -> None:
    """Guards the guard: the package does import them, so a walk that found
    nothing there would be looking at the wrong files."""
    found = set().union(*(_imported_roots(path) for path in _python_files(AI_DIR)))

    assert {"torch", "transformers", "huggingface_hub"} <= found
    assert len(_python_files(APP_DIR)) > 50


@pytest.mark.parametrize("library", MODEL_LIBRARIES)
def test_no_module_outside_the_ai_package_imports_a_model_library(
    library: str,
) -> None:
    violations = [
        str(path.relative_to(BACKEND_DIR))
        for path in _python_files(APP_DIR)
        if AI_DIR not in path.parents and library in _imported_roots(path)
    ]

    assert violations == [], (
        f"{library} is imported outside app/infrastructure/ai/; reach it through "
        "an adapter of that package, whose __init__ prepares its environment"
    )


def test_the_package_prepares_the_environment_before_any_other_import() -> None:
    """The order inside the `__init__` is the whole mechanism: the call must
    come before the first import of any other submodule of the package."""
    init = AI_DIR / "__init__.py"
    body = ast.parse(init.read_text(encoding="utf-8")).body

    call = next(
        index
        for index, node in enumerate(body)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "prepare_model_environment"
    )
    imports_before = [
        node.module
        for node in body[:call]
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("app.infrastructure.ai.")
        and node.module != "app.infrastructure.ai.environment"
    ]

    assert imports_before == []
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.infrastructure.ai.fake_embedding_model"
        for node in body[call + 1 :]
    )


def test_the_environment_module_imports_nothing_of_the_package_but_the_layout() -> None:
    """It runs before everything else in the package, so it may lean on
    nothing that could import a library first."""
    roots = _imported_roots(AI_DIR / "environment.py")

    assert not roots & set(MODEL_LIBRARIES)
    assert roots <= {"__future__", "os", "pathlib", "app"}
