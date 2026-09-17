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


def test_content_hash_defaults_to_none_meaning_unknown() -> None:
    """Defaulted so a writer that never hashed cannot claim it did.

    `None` reads as "cannot confirm unchanged", which costs one re-embed.
    A default of, say, the empty string would compare unequal to every
    real digest too, but would lose the ability to tell "never computed"
    apart from "computed and different".
    """
    metadata = IndexMetadata(file_size=1, file_modified_at=None)

    assert metadata.content_hash is None


def test_content_hash_is_stored_when_supplied() -> None:
    metadata = IndexMetadata(file_size=1, file_modified_at=None, content_hash="a" * 64)

    assert metadata.content_hash == "a" * 64


def test_content_hash_is_immutable() -> None:
    metadata = IndexMetadata(file_size=None, file_modified_at=None)

    with pytest.raises(FrozenInstanceError):
        metadata.content_hash = "b" * 64  # type: ignore[misc]


def test_metadata_with_the_same_fields_compares_equal() -> None:
    """The bulk prefetch is asserted against per-id reads by equality."""
    modified_at = datetime.datetime.now(datetime.UTC)

    assert IndexMetadata(1, modified_at, "a" * 64) == IndexMetadata(
        1, modified_at, "a" * 64
    )
    assert IndexMetadata(1, modified_at, "a" * 64) != IndexMetadata(
        1, modified_at, "b" * 64
    )


def test_thumbnail_path_defaults_to_none_meaning_no_thumbnail() -> None:
    """RFC-030: a row indexed before thumbnails existed has none.

    Defaulted like `content_hash`, so every construction site written
    before RFC-030 -- the pipeline's metadata refresh among them -- keeps
    describing a row without claiming a thumbnail it never rendered.
    """
    metadata = IndexMetadata(file_size=1, file_modified_at=None)

    assert metadata.thumbnail_path is None
    assert metadata.thumbnail_generated is False


def test_thumbnail_generated_follows_the_stored_location() -> None:
    metadata = IndexMetadata(
        file_size=1, file_modified_at=None, thumbnail_path="ab/example.jpg"
    )

    assert metadata.thumbnail_generated is True
