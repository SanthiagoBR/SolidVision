from __future__ import annotations

import uuid

import pytest

from app.domain.exceptions import InvalidImageIdentifierError
from app.domain.value_objects.image_id import ImageId


def test_image_id_accepts_valid_uuid() -> None:
    value = uuid.uuid4()

    image_id = ImageId(value)

    assert image_id.value == value


def test_image_id_accepts_valid_uuid_string() -> None:
    value = uuid.uuid4()

    image_id = ImageId(str(value))

    assert image_id.value == value


def test_image_id_rejects_empty_identifiers() -> None:
    with pytest.raises(InvalidImageIdentifierError):
        ImageId("")


def test_image_id_rejects_malformed_identifiers() -> None:
    with pytest.raises(InvalidImageIdentifierError):
        ImageId("not-a-uuid")


def test_image_id_compares_by_value() -> None:
    first = ImageId(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    second = ImageId(uuid.UUID("87654321-4321-8765-4321-876543218765"))

    assert first != second

    third = ImageId(first.value)

    assert first == third
    assert hash(first) == hash(third)
