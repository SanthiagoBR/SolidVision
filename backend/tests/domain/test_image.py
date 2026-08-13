from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath


def test_image_stores_required_attributes() -> None:
    image_id = ImageId(uuid.uuid4())
    image_path = ImagePath("images/example.png")

    image = Image(id=image_id, path=image_path, filename="example", extension="png")

    assert image.id == image_id
    assert image.path == image_path
    assert image.filename == "example"
    assert image.extension == "png"


def test_image_is_immutable() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    with pytest.raises(FrozenInstanceError):
        image.filename = "other"  # type: ignore[misc]


def test_image_compares_by_identity() -> None:
    image_id = ImageId(uuid.uuid4())
    first = Image(
        id=image_id, path=ImagePath("images/one.png"), filename="one", extension="png"
    )
    second = Image(
        id=image_id, path=ImagePath("images/two.png"), filename="two", extension="jpg"
    )

    assert first == second
    assert hash(first) == hash(second)


def test_image_has_no_infrastructure_behavior() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    assert not hasattr(image, "database")
    assert not hasattr(image, "session")


def test_image_has_no_embedding_field() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    assert not hasattr(image, "embedding")
