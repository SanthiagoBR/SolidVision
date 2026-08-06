from __future__ import annotations

import uuid

from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.database.models.image_model import ImageModel


def test_from_domain_copies_fields() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.id == image.id.value
    assert model.path == str(image.path)
    assert model.filename == image.filename
    assert model.extension == image.extension


def test_to_domain_reconstructs_value_objects() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)

    reconstructed = model.to_domain()

    assert reconstructed.id == image.id
    assert reconstructed.path == image.path
    assert reconstructed.filename == image.filename
    assert reconstructed.extension == image.extension
    assert isinstance(reconstructed.id, ImageId)
    assert isinstance(reconstructed.path, ImagePath)


def test_round_trip_preserves_fields_individually() -> None:
    original = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/roundtrip.png"),
        filename="roundtrip",
        extension="png",
    )

    reconstructed = ImageModel.from_domain(original).to_domain()

    assert reconstructed.id == original.id
    assert reconstructed.path == original.path
    assert reconstructed.filename == original.filename
    assert reconstructed.extension == original.extension
