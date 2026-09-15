"""The RFC-028 write half of `ImageRepository`, one contract, three implementations.

`test_search_similar_contract.py` holds the three repositories to one
*search*. This file holds them to one answer about *writing and reading back*
a capture date, which the search contract cannot see: whether the prefetch
reports a row's source, whether a metadata refresh erases it, whether a
missing row is quietly skipped. A double that got any of those wrong would
make every Application test of the conditional write evidence about the
double.
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
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
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

SHOT = datetime.datetime(2018, 12, 31, 23, 30)
ORIGINAL = CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)
MODIFIED_AT = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    """Each implementation in turn; the database only for its own round.

    `empty_db_session`, because one case searches, and a top-K query cannot
    ignore rows it did not create.
    """
    if request.param == "postgres":
        yield PostgresImageRepository(request.getfixturevalue("empty_db_session"))
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


def indexed(repository: ImageRepository, capture: CaptureDate | None = None) -> Image:
    unique = uuid.uuid4().hex
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"fotos/{unique}.jpg"),
        filename=unique,
        extension="jpg",
        captured_at=capture.captured_at if capture else None,
        capture_source=capture.source if capture else None,
    )
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=EmbeddingVector([0.1] * EMBEDDING_DIMENSION),
            file_size=1024,
            file_modified_at=MODIFIED_AT,
            content_hash="a" * 64,
        )
    )
    return image


def test_an_indexed_image_keeps_the_capture_date_it_was_saved_with(
    repository: ImageRepository,
) -> None:
    image = indexed(repository, ORIGINAL)

    stored = repository.get(image.id)

    assert stored is not None
    assert stored.capture_date == ORIGINAL
    assert stored.captured_at is not None
    assert stored.captured_at.tzinfo is None


def test_a_row_saved_without_a_capture_date_reads_back_never_examined(
    repository: ImageRepository,
) -> None:
    """NULL stays `None`; it is never promoted to `unknown` on the way out."""
    image = indexed(repository)

    metadata = repository.get_index_metadata(image.id)
    stored = repository.get(image.id)

    assert metadata is not None
    assert metadata.capture_source is None
    assert stored is not None
    assert stored.capture_source is None


def test_the_prefetch_reports_each_rows_capture_source(
    repository: ImageRepository,
) -> None:
    never = indexed(repository)
    unknown = indexed(repository, CaptureDate.unknown())
    dated = indexed(repository, ORIGINAL)

    metadata = repository.get_index_metadata_many([never.id, unknown.id, dated.id])

    assert metadata[never.id].capture_source is None
    assert metadata[unknown.id].capture_source is CaptureSource.UNKNOWN
    assert metadata[dated.id].capture_source is CaptureSource.EXIF_ORIGINAL


def test_update_capture_date_writes_the_date_and_its_source(
    repository: ImageRepository,
) -> None:
    image = indexed(repository)

    repository.update_capture_date(image.id, ORIGINAL)

    stored = repository.get(image.id)
    metadata = repository.get_index_metadata(image.id)
    assert stored is not None
    assert stored.capture_date == ORIGINAL
    assert metadata is not None
    assert metadata.capture_source is CaptureSource.EXIF_ORIGINAL


def test_update_capture_date_leaves_the_change_signals_alone(
    repository: ImageRepository,
) -> None:
    """A capture date is metadata about the photo, not about the file on disk."""
    image = indexed(repository)

    repository.update_capture_date(image.id, ORIGINAL)

    metadata = repository.get_index_metadata(image.id)
    assert metadata == IndexMetadata(
        file_size=1024,
        file_modified_at=MODIFIED_AT,
        content_hash="a" * 64,
        capture_source=CaptureSource.EXIF_ORIGINAL,
    )


def test_update_capture_date_keeps_the_image_searchable(
    repository: ImageRepository,
) -> None:
    """No embedding is touched, so the row is exactly as findable as before."""
    image = indexed(repository)

    repository.update_capture_date(image.id, ORIGINAL)

    hits = repository.search_similar(EmbeddingVector([0.1] * EMBEDDING_DIMENSION), 100)
    assert image in [hit.image for hit in hits]


def test_update_capture_date_for_a_missing_row_is_a_no_op(
    repository: ImageRepository,
) -> None:
    """The same contract as `update_index_metadata()`: no row, nothing created."""
    missing = ImageId(uuid.uuid4())

    repository.update_capture_date(missing, ORIGINAL)

    assert repository.get(missing) is None
    assert repository.get_index_metadata(missing) is None


def test_update_capture_date_many_writes_every_row_and_skips_missing_ones(
    repository: ImageRepository,
) -> None:
    first = indexed(repository)
    second = indexed(repository)
    missing = ImageId(uuid.uuid4())

    repository.update_capture_date_many(
        {first.id: ORIGINAL, second.id: CaptureDate.unknown(), missing: ORIGINAL}
    )

    metadata = repository.get_index_metadata_many([first.id, second.id, missing])
    assert metadata[first.id].capture_source is CaptureSource.EXIF_ORIGINAL
    assert metadata[second.id].capture_source is CaptureSource.UNKNOWN
    assert missing not in metadata


def test_update_capture_date_many_with_nothing_is_a_no_op(
    repository: ImageRepository,
) -> None:
    image = indexed(repository, ORIGINAL)

    repository.update_capture_date_many({})

    stored = repository.get(image.id)
    assert stored is not None
    assert stored.capture_date == ORIGINAL


def test_a_metadata_refresh_does_not_erase_the_capture_date(
    repository: ImageRepository,
) -> None:
    """`update_index_metadata()` writes change signals only.

    The `IndexMetadata` a refresh passes in has `capture_source=None`,
    because the use case builds it from the file's new mtime and size. An
    implementation that stored it whole would reset a dated row to "never
    examined" on every copy or restore of the file.
    """
    image = indexed(repository, ORIGINAL)

    repository.update_index_metadata(
        image.id,
        IndexMetadata(
            file_size=1024,
            file_modified_at=MODIFIED_AT + datetime.timedelta(days=1),
            content_hash="a" * 64,
        ),
    )

    metadata = repository.get_index_metadata(image.id)
    stored = repository.get(image.id)
    assert metadata is not None
    assert metadata.capture_source is CaptureSource.EXIF_ORIGINAL
    assert stored is not None
    assert stored.capture_date == ORIGINAL


def test_reindexing_changed_bytes_replaces_the_capture_date(
    repository: ImageRepository,
) -> None:
    """`save_indexed()` writes the whole row, capture date included.

    A date read from the old bytes no longer describes the file, so a
    record carrying none resets the row to "never examined".
    """
    image = indexed(repository, ORIGINAL)
    replacement = Image(
        id=image.id,
        device_id=image.device_id,
        relative_path=image.relative_path,
        filename=image.filename,
        extension=image.extension,
    )

    repository.save_indexed(
        IndexingRecord(
            image=replacement,
            embedding=EmbeddingVector([0.2] * EMBEDDING_DIMENSION),
            file_size=2048,
            file_modified_at=MODIFIED_AT,
            content_hash="b" * 64,
        )
    )

    metadata = repository.get_index_metadata(image.id)
    assert metadata is not None
    assert metadata.capture_source is None
