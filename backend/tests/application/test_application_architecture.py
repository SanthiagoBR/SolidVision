from __future__ import annotations

import ast
from pathlib import Path


def test_application_package_does_not_import_infrastructure_or_frameworks() -> None:
    application_dir = Path(__file__).resolve().parents[1] / "app" / "application"

    for path in application_dir.rglob("*.py"):
        if path.name == "__init__.py":
            continue

        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module.startswith("app.infrastructure")
                    or node.module.startswith("sqlalchemy")
                    or node.module.startswith("fastapi")
                    or node.module.startswith("pydantic")
                    or node.module.startswith("alembic")
                ):
                    raise AssertionError(
                        f"{path} imports forbidden module {node.module}"
                    )
