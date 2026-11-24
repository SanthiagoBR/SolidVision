from __future__ import annotations

import ast
from pathlib import Path

APPLICATION_DIR = Path(__file__).resolve().parents[2] / "app" / "application"

FORBIDDEN_PREFIXES = (
    "app.infrastructure",
    "app.presentation",
    "sqlalchemy",
    "fastapi",
    "starlette",
    "pydantic",
    "alembic",
)


def _is_forbidden(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in FORBIDDEN_PREFIXES
    )


def _imported_modules(tree: ast.AST) -> list[str]:
    """Every module named by `import x` and by `from x import y`, at any depth."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_application_package_does_not_import_infrastructure_or_frameworks() -> None:
    paths = sorted(APPLICATION_DIR.rglob("*.py"))
    # Without this, a wrong path would find no files and pass without checking.
    assert paths, f"no Python files found under {APPLICATION_DIR}"

    violations = [
        f"{path.relative_to(APPLICATION_DIR)} imports {module}"
        for path in paths
        for module in _imported_modules(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if _is_forbidden(module)
    ]

    assert not violations, "\n".join(violations)
