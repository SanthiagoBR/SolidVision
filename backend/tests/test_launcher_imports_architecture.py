"""Nothing heavy in the launcher; no `torch` in the import of the API (RFC-036 10).

The launcher supervises the processes that load the models and serve HTTP, and
must be neither: it imports no `torch`, `transformers`, `fastapi` or `uvicorn`
(section 4.1). A supervisor that imported FastAPI would start as slowly as the
API, and one that imported `torch` would take 14 to 40 s of a hard disk to
start supervising anything.

The check is transitive and real: every module the launcher's files import --
function-local imports included, because `__main__` imports most of what it
needs inside functions, after the lock -- is imported in a fresh process, and
`sys.modules` is read afterwards. The API's import is held to the half of
section 5.3 the same way: after it, neither `torch` nor `transformers` is in
`sys.modules`.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
LAUNCHER_DIR = BACKEND_DIR / "app" / "launcher"

HEAVY = ("torch", "transformers", "fastapi", "uvicorn")
MODEL_LIBRARIES = ("torch", "transformers")
PROCESS_SECONDS = 300


def _imported_by(path: Path) -> set[str]:
    """Every module a file imports, at the top or inside a function."""
    package = "app.launcher"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module)
            elif node.level == 1:
                if node.module:
                    found.add(f"{package}.{node.module}")
                else:
                    found.update(f"{package}.{alias.name}" for alias in node.names)
    return found


def _launcher_imports() -> set[str]:
    modules = {f"app.launcher.{path.stem}" for path in LAUNCHER_DIR.glob("*.py")} | {
        "app.launcher"
    }
    for path in LAUNCHER_DIR.glob("*.py"):
        modules |= _imported_by(path)
    modules.discard("app.launcher.__init__")
    return modules


def _loaded_after_importing(modules: set[str], watched: tuple[str, ...]) -> list[str]:
    script = (
        "import importlib, json, sys\n"
        f"for name in {sorted(modules)!r}:\n"
        "    importlib.import_module(name)\n"
        f"print(json.dumps(sorted(m for m in {watched!r} if m in sys.modules)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(BACKEND_DIR)},
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    loaded: list[str] = json.loads(completed.stdout.strip().splitlines()[-1])
    return loaded


def test_the_walk_finds_the_launcher_and_what_it_imports_late() -> None:
    """Guards the guard: the lazy imports of `__main__` are part of the graph."""
    modules = _launcher_imports()

    assert "app.launcher.supervisor" in modules
    assert "app.infrastructure.config.settings" in modules
    assert "app.infrastructure.database.embedded.cluster" in modules
    assert len(list(LAUNCHER_DIR.glob("*.py"))) >= 7


def test_the_launcher_imports_nothing_heavy() -> None:
    loaded = _loaded_after_importing(_launcher_imports(), HEAVY)

    assert loaded == [], (
        f"the launcher's import graph reaches {loaded}; supervising must stay "
        "lighter than what it supervises"
    )


def test_the_launcher_reaches_for_no_model_and_no_route() -> None:
    """The same rule, spelt in the source, so that a lazy import that the
    process check above would only meet on a path it does not take is still
    caught."""
    forbidden = ("app.presentation", "app.infrastructure.ai")
    violations = sorted(
        module
        for module in _launcher_imports()
        if module.split(".")[0] in HEAVY or module.startswith(forbidden)
    )

    assert violations == []


def test_importing_the_api_imports_no_model_library() -> None:
    """Section 5.3: the port opens in the time of an import without `torch`."""
    loaded = _loaded_after_importing({"app.presentation.api"}, MODEL_LIBRARIES)

    assert loaded == []
