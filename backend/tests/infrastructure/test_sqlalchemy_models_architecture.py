from __future__ import annotations

import ast
from pathlib import Path

from app.infrastructure.database.models.image_model import ImageModel


def test_image_model_uses_expected_table_metadata() -> None:
    assert ImageModel.__tablename__ == "images"
    assert ImageModel.__table__.primary_key is not None
    assert ImageModel.__table__.c.path.unique is True
    assert "embedding" not in ImageModel.__table__.c
    assert "collection_name" not in ImageModel.__table__.c


def test_domain_imports_do_not_depend_on_sqlalchemy() -> None:
    domain_dir = Path(__file__).resolve().parents[1] / "app" / "domain"

    for path in domain_dir.rglob("*.py"):
        if path.name == "__init__.py":
            continue

        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("sqlalchemy"):
                    raise AssertionError(
                        f"{path} imports SQLAlchemy module {node.module}"
                    )


def test_image_model_imports_domain() -> None:
    import inspect

    source = inspect.getsource(ImageModel)
    assert "Image" in source
    assert "ImageId" in source
    assert "ImagePath" in source


def test_image_model_is_not_imported_by_domain() -> None:
    import app.domain as domain_package

    assert not hasattr(domain_package, "ImageModel")
