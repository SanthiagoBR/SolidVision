from __future__ import annotations

import datetime
import uuid

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)


def _build_image(path: str = "images/example.png") -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath(path),
        filename="example",
        extension="png",
    )


def test_in_memory_image_repository_implements_repository_interface() -> None:
    repository = InMemoryImageRepository()
    assert isinstance(repository, ImageRepository)


def test_in_memory_image_repository_persists_and_reads_images() -> None:
    repository = InMemoryImageRepository()
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    repository.save(image)

    assert repository.get(image.id) == image
    assert repository.exists(image.id) is True
    assert repository.list() == [image]


def test_in_memory_image_repository_deletes_and_lists_images() -> None:
    repository = InMemoryImageRepository()
    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    repository.save(image)
    repository.delete(image.id)

    assert repository.get(image.id) is None
    assert repository.exists(image.id) is False
    assert repository.list() == []


def test_get_index_metadata_returns_none_for_missing_image() -> None:
    repository = InMemoryImageRepository()

    assert repository.get_index_metadata(ImageId(uuid.uuid4())) is None


def test_get_index_metadata_returns_none_pair_for_image_saved_without_metadata() -> (
    None
):
    repository = InMemoryImageRepository()
    image = _build_image()
    repository.save(image)

    metadata = repository.get_index_metadata(image.id)

    assert metadata is not None
    assert metadata.file_size is None
    assert metadata.file_modified_at is None


def test_save_indexed_persists_embedding_and_metadata() -> None:
    repository = InMemoryImageRepository()
    image = _build_image()
    modified_at = datetime.datetime.now(datetime.UTC)
    record = IndexingRecord(
        image=image,
        embedding=EmbeddingVector([0.1, 0.2]),
        file_size=1024,
        file_modified_at=modified_at,
    )

    repository.save_indexed(record)

    assert repository.get(image.id) == image
    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.file_size == 1024
    assert metadata.file_modified_at == modified_at


def test_save_indexed_updates_existing_image_without_duplicating() -> None:
    repository = InMemoryImageRepository()
    image = _build_image()
    first_record = IndexingRecord(
        image=image,
        embedding=EmbeddingVector([0.1]),
        file_size=100,
        file_modified_at=datetime.datetime.now(datetime.UTC),
    )
    repository.save_indexed(first_record)

    updated_modified_at = datetime.datetime.now(datetime.UTC)
    second_record = IndexingRecord(
        image=image,
        embedding=EmbeddingVector([0.9]),
        file_size=200,
        file_modified_at=updated_modified_at,
    )
    repository.save_indexed(second_record)

    assert repository.list() == [image]
    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.file_size == 200
    assert metadata.file_modified_at == updated_modified_at
