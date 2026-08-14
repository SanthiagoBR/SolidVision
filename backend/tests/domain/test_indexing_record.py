from __future__ import annotations

import datetime
import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord


def _build_image() -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )


def test_indexing_record_stores_all_fields() -> None:
    image = _build_image()
    embedding = EmbeddingVector([0.1, 0.2, 0.3])
    modified_at = datetime.datetime.now(datetime.UTC)

    record = IndexingRecord(
        image=image,
        embedding=embedding,
        file_size=1024,
        file_modified_at=modified_at,
    )

    assert record.image == image
    assert record.embedding == embedding
    assert record.file_size == 1024
    assert record.file_modified_at == modified_at


def test_indexing_record_accepts_none_metadata() -> None:
    record = IndexingRecord(
        image=_build_image(),
        embedding=EmbeddingVector([0.1]),
        file_size=None,
        file_modified_at=None,
    )

    assert record.file_size is None
    assert record.file_modified_at is None


def test_indexing_record_is_immutable() -> None:
    record = IndexingRecord(
        image=_build_image(),
        embedding=EmbeddingVector([0.1]),
        file_size=None,
        file_modified_at=None,
    )

    with pytest.raises(FrozenInstanceError):
        record.file_size = 10  # type: ignore[misc]
