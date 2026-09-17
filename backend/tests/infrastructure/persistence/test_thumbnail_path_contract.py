"""RFC-030's thumbnail column of `ImageRepository`, held to one contract.

What the thumbnail pipeline relies on and neither the search contract nor
the capture-date contract can see: that a location written with an indexed
image reads back through the prefetch, that re-indexing new bytes without a
thumbnail *clears* the old location rather than keeping it, that a metadata
refresh leaves it alone, and that the backfill's write skips ids with no
row. A double that got any of those wrong would make the Application tests
of the backfill evidence about the double.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import EMBEDDING_DIMENSION
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)

MODIFIED_AT = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    if request.param == "postgres":
        yield PostgresImageRepository(request.getfixturevalue("db_session"))
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


def new_image() -> Image:
    unique = uuid.uuid4().hex
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"fotos/{unique}.jpg"),
        filename=unique,
        extension="jpg",
    )


def record(
    image: Image, thumbnail_path: str | None, content_hash: str = "a" * 64
) -> IndexingRecord:
    return IndexingRecord(
        image=image,
        embedding=EmbeddingVector([0.1] * EMBEDDING_DIMENSION),
        file_size=1024,
        file_modified_at=MODIFIED_AT,
        content_hash=content_hash,
        thumbnail_path=thumbnail_path,
    )


def test_a_location_saved_with_the_image_reads_back_through_both_prefetches(
    repository: ImageRepository,
) -> None:
    image = new_image()
    repository.save_indexed(record(image, "ab/some-id.jpg"))

    single = repository.get_index_metadata(image.id)
    many = repository.get_index_metadata_many([image.id])

    assert single is not None
    assert single.thumbnail_path == "ab/some-id.jpg"
    assert single.thumbnail_generated
    assert many[image.id].thumbnail_path == "ab/some-id.jpg"


def test_an_image_saved_without_a_thumbnail_reads_back_without_one(
    repository: ImageRepository,
) -> None:
    image = new_image()
    repository.save_indexed_many([record(image, None)])

    metadata = repository.get_index_metadata(image.id)

    assert metadata is not None
    assert metadata.thumbnail_path is None
    assert not metadata.thumbnail_generated


def test_reindexing_new_bytes_without_a_thumbnail_clears_the_old_location(
    repository: ImageRepository,
) -> None:
    """The stale thumbnail must not survive under the new content hash.

    `GET /thumbnail` answers with `ETag: "<content_hash>"`. Keeping the old
    location beside a new hash would serve the old picture labelled as the
    new one, and a client revalidating would be told it is current.
    """
    image = new_image()
    repository.save_indexed(record(image, "ab/some-id.jpg", content_hash="a" * 64))

    repository.save_indexed_many([record(image, None, content_hash="b" * 64)])

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.content_hash == "b" * 64
    assert metadata.thumbnail_path is None


def test_a_metadata_refresh_leaves_the_thumbnail_alone(
    repository: ImageRepository,
) -> None:
    """Refresh means the bytes did not change, so the thumbnail still depicts them.

    The caller builds its `IndexMetadata` without a thumbnail, as the
    pipeline does; an implementation that stored that object wholesale
    would erase the location.
    """
    image = new_image()
    repository.save_indexed(record(image, "ab/some-id.jpg"))

    repository.update_index_metadata(
        image.id,
        IndexMetadata(
            file_size=2048,
            file_modified_at=MODIFIED_AT + datetime.timedelta(days=1),
            content_hash="a" * 64,
        ),
    )

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.file_size == 2048
    assert metadata.thumbnail_path == "ab/some-id.jpg"


def test_update_thumbnail_path_writes_only_the_location(
    repository: ImageRepository,
) -> None:
    image = new_image()
    repository.save_indexed(record(image, None))

    repository.update_thumbnail_path(image.id, "cd/other.jpg")

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.thumbnail_path == "cd/other.jpg"
    assert metadata.file_size == 1024
    assert metadata.content_hash == "a" * 64


def test_update_thumbnail_path_many_writes_each_and_skips_unknown_ids(
    repository: ImageRepository,
) -> None:
    first = new_image()
    second = new_image()
    repository.save_indexed_many([record(first, None), record(second, None)])
    stranger = ImageId(uuid.uuid4())

    repository.update_thumbnail_path_many(
        {first.id: "01/first.jpg", second.id: "02/second.jpg", stranger: "x.jpg"}
    )

    metadata = repository.get_index_metadata_many([first.id, second.id, stranger])
    assert metadata[first.id].thumbnail_path == "01/first.jpg"
    assert metadata[second.id].thumbnail_path == "02/second.jpg"
    assert stranger not in metadata


def test_an_empty_mapping_writes_nothing(repository: ImageRepository) -> None:
    image = new_image()
    repository.save_indexed(record(image, "ab/some-id.jpg"))

    repository.update_thumbnail_path_many({})

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.thumbnail_path == "ab/some-id.jpg"
