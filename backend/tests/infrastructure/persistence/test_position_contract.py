"""The RFC-032 write half of `ImageRepository`: one contract, three implementations.

`test_position_filter_contract.py` holds the three repositories to one
*search*; this file holds them to one answer about writing and reading back a
position, which the search cannot see: whether the prefetch reports a row's
source, whether a metadata refresh or a date write erases it, whether a
missing row is quietly skipped. The shape is `test_capture_date_contract.py`'s,
on purpose -- the two facts follow the same rules and must be checked the same
way.
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
from app.domain.value_objects.position import Position, PositionReading
from app.domain.value_objects.position_source import PositionSource
from app.infrastructure.database.models.image_model import EMBEDDING_DIMENSION
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)

FARM = PositionReading(Position(-26.321406, -48.816307), PositionSource.EXIF_GPS)
MOVED = PositionReading(Position(-26.551, -49.133), PositionSource.EXIF_GPS)
UNKNOWN = PositionReading.unknown()
ORIGINAL = CaptureDate(
    datetime.datetime(2018, 7, 14, 15, 32, 5), CaptureSource.EXIF_ORIGINAL
)
MODIFIED_AT = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    """Each implementation in turn; the database only for its own round."""
    if request.param == "postgres":
        yield PostgresImageRepository(request.getfixturevalue("empty_db_session"))
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


def indexed(
    repository: ImageRepository,
    position: PositionReading | None = None,
    capture: CaptureDate | None = None,
) -> Image:
    unique = uuid.uuid4().hex
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"fotos/{unique}.jpg"),
        filename=unique,
        extension="jpg",
        captured_at=capture.captured_at if capture else None,
        capture_source=capture.source if capture else None,
        latitude=position.latitude if position else None,
        longitude=position.longitude if position else None,
        position_source=position.source if position else None,
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


def test_an_indexed_image_keeps_the_position_it_was_saved_with(
    repository: ImageRepository,
) -> None:
    image = indexed(repository, FARM)

    stored = repository.get(image.id)

    assert stored is not None
    assert stored.position_reading == FARM
    assert stored.latitude == FARM.latitude
    assert stored.longitude == FARM.longitude


def test_double_precision_keeps_every_digit_a_gnss_records(
    repository: ImageRepository,
) -> None:
    """1e-7 degree is about a centimetre; nothing is rounded away on the round trip."""
    precise = PositionReading(
        Position(-26.3214063055556, -48.8163073333333), PositionSource.EXIF_GPS
    )
    image = indexed(repository, precise)

    stored = repository.get(image.id)

    assert stored is not None
    assert stored.latitude == precise.latitude
    assert stored.longitude == precise.longitude


def test_a_row_saved_without_a_position_reads_back_never_examined(
    repository: ImageRepository,
) -> None:
    """NULL stays `None`; it is never promoted to `unknown` on the way out."""
    image = indexed(repository)

    metadata = repository.get_index_metadata(image.id)
    stored = repository.get(image.id)

    assert metadata is not None
    assert metadata.position_source is None
    assert stored is not None
    assert stored.position_source is None
    assert stored.position_reading is None


def test_the_prefetch_reports_each_rows_position_source(
    repository: ImageRepository,
) -> None:
    never = indexed(repository)
    unknown = indexed(repository, UNKNOWN)
    placed = indexed(repository, FARM)

    metadata = repository.get_index_metadata_many([never.id, unknown.id, placed.id])

    assert metadata[never.id].position_source is None
    assert metadata[unknown.id].position_source is PositionSource.UNKNOWN
    assert metadata[placed.id].position_source is PositionSource.EXIF_GPS


def test_update_position_writes_all_three_columns(
    repository: ImageRepository,
) -> None:
    image = indexed(repository)

    repository.update_position(image.id, FARM)

    stored = repository.get(image.id)
    metadata = repository.get_index_metadata(image.id)
    assert stored is not None
    assert stored.position_reading == FARM
    assert metadata is not None
    assert metadata.position_source is PositionSource.EXIF_GPS


def test_update_position_can_record_examined_without_a_position(
    repository: ImageRepository,
) -> None:
    image = indexed(repository)

    repository.update_position(image.id, UNKNOWN)

    stored = repository.get(image.id)
    assert stored is not None
    assert stored.position_reading == UNKNOWN
    assert stored.latitude is None
    assert stored.longitude is None


def test_update_position_leaves_the_change_signals_and_the_date_alone(
    repository: ImageRepository,
) -> None:
    """A position is about the photo, not about the file on disk."""
    image = indexed(repository, capture=ORIGINAL)

    repository.update_position(image.id, FARM)

    assert repository.get_index_metadata(image.id) == IndexMetadata(
        file_size=1024,
        file_modified_at=MODIFIED_AT,
        content_hash="a" * 64,
        capture_source=CaptureSource.EXIF_ORIGINAL,
        position_source=PositionSource.EXIF_GPS,
    )
    stored = repository.get(image.id)
    assert stored is not None
    assert stored.capture_date == ORIGINAL


def test_a_date_write_leaves_the_position_alone(
    repository: ImageRepository,
) -> None:
    """The two facts are written by separate calls, and neither touches the other."""
    image = indexed(repository, FARM)

    repository.update_capture_date(image.id, ORIGINAL)

    stored = repository.get(image.id)
    assert stored is not None
    assert stored.position_reading == FARM


def test_update_position_keeps_the_image_searchable(
    repository: ImageRepository,
) -> None:
    image = indexed(repository)

    repository.update_position(image.id, FARM)

    hits = repository.search_similar(EmbeddingVector([0.1] * EMBEDDING_DIMENSION), 100)
    assert image in [hit.image for hit in hits]


def test_update_position_for_a_missing_row_is_a_no_op(
    repository: ImageRepository,
) -> None:
    missing = ImageId(uuid.uuid4())

    repository.update_position(missing, FARM)

    assert repository.get(missing) is None
    assert repository.get_index_metadata(missing) is None


def test_update_position_many_writes_every_row_and_skips_missing_ones(
    repository: ImageRepository,
) -> None:
    first = indexed(repository)
    second = indexed(repository)
    missing = ImageId(uuid.uuid4())

    repository.update_position_many({first.id: FARM, second.id: UNKNOWN, missing: FARM})

    metadata = repository.get_index_metadata_many([first.id, second.id, missing])
    assert metadata[first.id].position_source is PositionSource.EXIF_GPS
    assert metadata[second.id].position_source is PositionSource.UNKNOWN
    assert missing not in metadata


def test_update_position_many_with_nothing_is_a_no_op(
    repository: ImageRepository,
) -> None:
    image = indexed(repository, FARM)

    repository.update_position_many({})

    stored = repository.get(image.id)
    assert stored is not None
    assert stored.position_reading == FARM


def test_a_force_rewrite_replaces_one_position_with_another(
    repository: ImageRepository,
) -> None:
    image = indexed(repository, FARM)

    repository.update_position(image.id, MOVED)

    stored = repository.get(image.id)
    assert stored is not None
    assert stored.position_reading == MOVED


def test_a_metadata_refresh_does_not_erase_the_position(
    repository: ImageRepository,
) -> None:
    """`update_index_metadata()` writes change signals only (RFC-032 section 8).

    The `IndexMetadata` a refresh passes in has `position_source=None`. An
    implementation that stored it whole would reset a placed row to "never
    examined" on every copy of the file between disks -- the central
    operation of the collection.
    """
    image = indexed(repository, FARM)

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
    assert metadata.position_source is PositionSource.EXIF_GPS
    assert stored is not None
    assert stored.position_reading == FARM


def test_reindexing_changed_bytes_replaces_the_position(
    repository: ImageRepository,
) -> None:
    """`save_indexed()` writes the whole row from the entity, position included."""
    image = indexed(repository, FARM)
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

    stored = repository.get(image.id)
    assert stored is not None
    assert stored.position_reading is None
