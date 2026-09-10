from __future__ import annotations

import datetime
import uuid

from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)


def _build_image(path: str = "images/example.png") -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(path),
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
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
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
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
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


def test_save_indexed_records_the_content_hash() -> None:
    repository = InMemoryImageRepository()
    image = _build_image()
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=EmbeddingVector([0.1]),
            file_size=10,
            file_modified_at=datetime.datetime.now(datetime.UTC),
            content_hash="a" * 64,
        )
    )

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.content_hash == "a" * 64


def test_get_index_metadata_many_agrees_with_the_per_id_reads() -> None:
    repository = InMemoryImageRepository()
    images = [_build_image(f"images/{index}.png") for index in range(3)]
    for index, image in enumerate(images):
        repository.save_indexed(
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.1]),
                file_size=index,
                file_modified_at=datetime.datetime.now(datetime.UTC),
                content_hash=f"{index:064d}",
            )
        )
    ids = [image.id for image in images]

    bulk = repository.get_index_metadata_many(ids)

    assert bulk == {
        image_id: repository.get_index_metadata(image_id) for image_id in ids
    }


def test_get_index_metadata_many_omits_unknown_ids() -> None:
    repository = InMemoryImageRepository()
    known = _build_image()
    repository.save(known)
    unknown = ImageId(uuid.uuid4())

    result = repository.get_index_metadata_many([known.id, unknown])

    assert set(result) == {known.id}


def test_get_index_metadata_many_with_no_ids_returns_an_empty_mapping() -> None:
    assert InMemoryImageRepository().get_index_metadata_many([]) == {}


def test_update_index_metadata_replaces_the_stored_metadata() -> None:
    repository = InMemoryImageRepository()
    image = _build_image()
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=EmbeddingVector([0.1]),
            file_size=10,
            file_modified_at=datetime.datetime.now(datetime.UTC),
            content_hash="a" * 64,
        )
    )
    refreshed_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)

    repository.update_index_metadata(
        image.id,
        IndexMetadata(
            file_size=20, file_modified_at=refreshed_at, content_hash="b" * 64
        ),
    )

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.file_size == 20
    assert metadata.file_modified_at == refreshed_at
    assert metadata.content_hash == "b" * 64


def test_update_index_metadata_leaves_the_image_in_place() -> None:
    repository = InMemoryImageRepository()
    image = _build_image()
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=EmbeddingVector([0.1]),
            file_size=10,
            file_modified_at=None,
            content_hash=None,
        )
    )

    repository.update_index_metadata(
        image.id,
        IndexMetadata(file_size=20, file_modified_at=None, content_hash="c" * 64),
    )

    assert repository.list() == [image]


def test_update_index_metadata_on_a_missing_image_is_a_no_op() -> None:
    repository = InMemoryImageRepository()
    missing = ImageId(uuid.uuid4())

    repository.update_index_metadata(
        missing,
        IndexMetadata(file_size=1, file_modified_at=None, content_hash=None),
    )

    assert repository.get_index_metadata(missing) is None
    assert repository.list() == []


def test_save_indexed_many_persists_every_record() -> None:
    repository = InMemoryImageRepository()
    images = [_build_image(f"images/{index}.png") for index in range(4)]

    repository.save_indexed_many(
        [
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.1]),
                file_size=index,
                file_modified_at=None,
                content_hash=f"{index:064d}",
            )
            for index, image in enumerate(images)
        ]
    )

    assert len(repository.list()) == 4
    for index, image in enumerate(images):
        metadata = repository.get_index_metadata(image.id)
        assert metadata is not None
        assert metadata.file_size == index
        assert metadata.content_hash == f"{index:064d}"


def test_save_indexed_many_upserts_rather_than_duplicating() -> None:
    repository = InMemoryImageRepository()
    image = _build_image()
    record = IndexingRecord(
        image=image,
        embedding=EmbeddingVector([0.1]),
        file_size=1,
        file_modified_at=None,
    )
    repository.save_indexed(record)

    repository.save_indexed_many(
        [
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.9]),
                file_size=2,
                file_modified_at=None,
            )
        ]
    )

    assert repository.list() == [image]
    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.file_size == 2


def test_save_indexed_many_with_no_records_is_a_no_op() -> None:
    repository = InMemoryImageRepository()

    repository.save_indexed_many([])

    assert repository.list() == []
