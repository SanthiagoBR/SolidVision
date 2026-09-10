from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.database.models.image_model import ImageModel


def test_from_domain_copies_fields() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.id == image.id.value
    assert model.device_id == image.device_id.value
    assert model.relative_path == str(image.relative_path)
    assert model.filename == image.filename
    assert model.extension == image.extension


def test_from_domain_leaves_embedding_unset() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.embedding is None


def test_from_domain_leaves_incremental_metadata_unset() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.file_size is None
    assert model.file_modified_at is None


def test_to_domain_reconstructs_value_objects() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)

    reconstructed = model.to_domain()

    assert reconstructed.id == image.id
    assert reconstructed.device_id == image.device_id
    assert reconstructed.relative_path == image.relative_path
    assert reconstructed.filename == image.filename
    assert reconstructed.extension == image.extension
    assert isinstance(reconstructed.id, ImageId)
    assert isinstance(reconstructed.relative_path, ImagePath)
    assert isinstance(reconstructed.device_id, DeviceId)


def test_to_domain_ignores_unset_embedding() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    assert model.embedding is None

    reconstructed = model.to_domain()

    assert not hasattr(reconstructed, "embedding")
    assert reconstructed == image


def test_to_domain_ignores_populated_embedding() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    model.embedding = [0.1] * 512

    reconstructed = model.to_domain()

    assert not hasattr(reconstructed, "embedding")
    assert reconstructed == image


def test_to_domain_ignores_populated_incremental_metadata() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    model.file_size = 1024
    model.file_modified_at = datetime.datetime.now(datetime.UTC)

    reconstructed = model.to_domain()

    assert not hasattr(reconstructed, "file_size")
    assert not hasattr(reconstructed, "file_modified_at")
    assert reconstructed == image


def test_round_trip_preserves_fields_individually() -> None:
    original = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/roundtrip.png"),
        filename="roundtrip",
        extension="png",
    )

    reconstructed = ImageModel.from_domain(original).to_domain()

    assert reconstructed.id == original.id
    assert reconstructed.device_id == original.device_id
    assert reconstructed.relative_path == original.relative_path
    assert reconstructed.filename == original.filename
    assert reconstructed.extension == original.extension


def test_row_persists_with_incremental_metadata_left_none(
    db_session: Session,
) -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/{uuid.uuid4().hex}.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)

    db_session.add(model)
    db_session.commit()

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.file_size is None
    assert row.file_modified_at is None


def test_negative_file_size_raises_check_constraint_violation(
    db_session: Session,
) -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/{uuid.uuid4().hex}.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    model.file_size = -1

    db_session.add(model)
    with pytest.raises(IntegrityError):
        db_session.commit()
