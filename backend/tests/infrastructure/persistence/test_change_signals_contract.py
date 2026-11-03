"""RFC-045's snapshot of a device, held to one contract by all three repositories.

`list_change_signals()` is what the read-ahead thread predicts from: if a
double answered differently from PostgreSQL -- a row with no metadata left
out, another device's rows let in, a refreshed mtime not seen -- the tests of
the thread would be evidence about the double.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from tests.application.fakes import FakeImageRepository, make_device
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import EMBEDDING_DIMENSION
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)

MODIFIED_AT = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)
OTHER_DEVICE = make_device(
    "\\\\?\\Volume{0000beef-0000-0000-0000-000000000000}\\", label="OTHER"
)


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    if request.param == "postgres":
        session = request.getfixturevalue("empty_db_session")
        PostgresDeviceRepository(session).save(OTHER_DEVICE)
        yield PostgresImageRepository(session)
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


def image(name: str, device_id: DeviceId = TEST_DEVICE_ID) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=device_id,
        relative_path=ImagePath(f"fotos/{name}.jpg"),
        filename=name,
        extension="jpg",
    )


def indexed(
    repository: ImageRepository,
    picture: Image,
    file_size: int | None = 1024,
    file_modified_at: datetime.datetime | None = MODIFIED_AT,
) -> Image:
    repository.save_indexed(
        IndexingRecord(
            image=picture,
            embedding=EmbeddingVector([0.1] * EMBEDDING_DIMENSION),
            file_size=file_size,
            file_modified_at=file_modified_at,
            content_hash="a" * 64,
        )
    )
    return picture


def test_every_row_of_the_device_with_its_size_and_mtime(
    repository: ImageRepository,
) -> None:
    first = indexed(repository, image("a"), 1024, MODIFIED_AT)
    second = indexed(
        repository, image("b"), 2048, MODIFIED_AT + datetime.timedelta(days=1)
    )

    assert repository.list_change_signals(TEST_DEVICE_ID) == {
        first.id: ChangeSignals(1024, MODIFIED_AT),
        second.id: ChangeSignals(2048, MODIFIED_AT + datetime.timedelta(days=1)),
    }


def test_the_rows_of_another_device_are_left_out(repository: ImageRepository) -> None:
    mine = indexed(repository, image("mine"))
    indexed(repository, image("theirs", OTHER_DEVICE.id))

    assert set(repository.list_change_signals(TEST_DEVICE_ID)) == {mine.id}


def test_a_row_that_never_had_the_signals_reads_back_as_none(
    repository: ImageRepository,
) -> None:
    """Treated as changed by the thread, exactly as the decision treats it."""
    legacy = indexed(repository, image("legacy"), None, None)

    assert repository.list_change_signals(TEST_DEVICE_ID) == {
        legacy.id: ChangeSignals(None, None)
    }


def test_a_refresh_is_seen(repository: ImageRepository) -> None:
    picture = indexed(repository, image("touched"))
    later = MODIFIED_AT + datetime.timedelta(hours=3)

    repository.update_index_metadata(picture.id, IndexMetadata(1024, later, "a" * 64))

    assert repository.list_change_signals(TEST_DEVICE_ID) == {
        picture.id: ChangeSignals(1024, later)
    }


def test_an_empty_device_has_an_empty_snapshot(repository: ImageRepository) -> None:
    assert repository.list_change_signals(TEST_DEVICE_ID) == {}
