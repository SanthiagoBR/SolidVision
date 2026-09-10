from __future__ import annotations

import ast
from pathlib import Path
from typing import cast

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Table, UniqueConstraint

from app.infrastructure.config.settings import settings
from app.infrastructure.database.models.device_model import DeviceModel
from app.infrastructure.database.models.image_model import ImageModel


def test_image_model_uses_expected_table_metadata() -> None:
    assert ImageModel.__tablename__ == "images"
    assert ImageModel.__table__.primary_key is not None
    assert "collection_name" not in ImageModel.__table__.c


def test_image_model_has_no_absolute_path_column() -> None:
    """RFC-027: the column that changed without anybody writing to it.

    An absolute path on Windows starts with a drive letter, and a drive
    letter is assigned by mount order -- so the stored location of a file
    nobody touched moved on its own, and the `uuid5` id derived from it
    moved with it. There is no `path`, `filepath`, `drive_letter` or
    `mount_point` column, and none may come back.
    """
    columns = set(ImageModel.__table__.c.keys())

    assert "path" not in columns
    assert "filepath" not in columns
    assert "drive_letter" not in columns
    assert "mount_point" not in columns


def test_images_are_unique_per_device_and_relative_path() -> None:
    """The constraint that means what `UNIQUE (path)` only looked like.

    `D:/fotos/x.JPG` and `F:/fotos/x.JPG` are two different strings, so
    the old constraint accepted the same file on the same disk twice.
    """
    table = cast(Table, ImageModel.__table__)
    unique_constraints = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert unique_constraints == {
        "uq_images_device_relative_path": ["device_id", "relative_path"]
    }


def test_image_model_device_id_is_a_foreign_key_to_devices() -> None:
    (foreign_key,) = list(ImageModel.__table__.c.device_id.foreign_keys)

    assert foreign_key.column.table.name == "devices"
    assert ImageModel.__table__.c.device_id.nullable is False
    assert ImageModel.__table__.c.relative_path.nullable is False


def test_device_model_stores_no_mount_point_or_connection_state() -> None:
    """RFC-027 sections 4 and 7, as schema rather than as prose.

    A mount point is unstable and a connection flag is unmaintainable --
    Windows never tells this process that a disk was unplugged, so the
    column would be wrong from that moment until nothing corrected it.
    """
    columns = set(DeviceModel.__table__.c.keys())

    assert "mount_point" not in columns
    assert "drive_letter" not in columns
    assert "is_connected" not in columns
    assert "connected" not in columns


def test_device_model_volume_identity_is_unique_and_kinded() -> None:
    assert DeviceModel.__table__.c.volume_identity.unique is True
    assert DeviceModel.__table__.c.volume_identity.nullable is False
    assert DeviceModel.__table__.c.volume_kind.nullable is False


def test_device_model_keeps_the_scan_denominator() -> None:
    """RFC-027 section 8: "% indexed" needs a denominator of its own.

    Counted from rows in `images` instead, the figure could only ever
    describe files somebody already scanned -- it reaches 100% after any
    complete run and never sees a file nobody looked at.
    """
    assert "last_scan_at" in DeviceModel.__table__.c
    assert DeviceModel.__table__.c.last_scan_file_count.nullable is True


def test_image_model_embedding_column_matches_pgvector_schema() -> None:
    embedding_column = ImageModel.__table__.c.embedding

    assert isinstance(embedding_column.type, Vector)
    assert embedding_column.type.dim == 512
    assert embedding_column.nullable is True


def test_image_model_embedding_column_matches_configured_dimension() -> None:
    """The mapped column and the configured model must not drift apart.

    The column literal is pinned by the RFC-023 migration and the setting
    is what the CLIP adapter validates its output against, so the two are
    written independently on purpose. This is the guard that catches a
    change to one without the other -- which would mean the application
    generating vectors the database cannot store.
    """
    embedding_type = ImageModel.__table__.c.embedding.type
    assert isinstance(embedding_type, Vector)

    assert embedding_type.dim == settings.embedding_dimension


def test_image_model_file_size_column_matches_schema() -> None:
    file_size_column = ImageModel.__table__.c.file_size

    assert isinstance(file_size_column.type, BigInteger)
    assert file_size_column.nullable is True


def test_image_model_file_modified_at_column_matches_schema() -> None:
    file_modified_at_column = ImageModel.__table__.c.file_modified_at

    assert isinstance(file_modified_at_column.type, DateTime)
    assert file_modified_at_column.type.timezone is True
    assert file_modified_at_column.nullable is True


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
    assert "DeviceId" in source


def test_image_model_is_not_imported_by_domain() -> None:
    import app.domain as domain_package

    assert not hasattr(domain_package, "ImageModel")
