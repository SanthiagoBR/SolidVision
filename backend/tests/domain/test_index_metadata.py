from __future__ import annotations

import datetime
from dataclasses import FrozenInstanceError

import pytest

from app.domain.value_objects.index_metadata import IndexMetadata


def test_index_metadata_stores_fields() -> None:
    modified_at = datetime.datetime.now(datetime.UTC)

    metadata = IndexMetadata(file_size=1024, file_modified_at=modified_at)

    assert metadata.file_size == 1024
    assert metadata.file_modified_at == modified_at


def test_index_metadata_accepts_none_fields() -> None:
    metadata = IndexMetadata(file_size=None, file_modified_at=None)

    assert metadata.file_size is None
    assert metadata.file_modified_at is None


def test_index_metadata_is_immutable() -> None:
    metadata = IndexMetadata(file_size=None, file_modified_at=None)

    with pytest.raises(FrozenInstanceError):
        metadata.file_size = 10  # type: ignore[misc]


def test_index_metadata_does_not_carry_image_or_embedding() -> None:
    metadata = IndexMetadata(file_size=None, file_modified_at=None)

    assert not hasattr(metadata, "image")
    assert not hasattr(metadata, "embedding")
