"""The photos a scan no longer found, held to one contract by all three repositories.

RFC-046 sections 5 and 6: a marked row is hidden from the eight reads that
serve the interface, comes back when it is indexed again, and is deleted only
by the purge. If a double hid a row PostgreSQL showed -- or the other way
round -- every Application test above it would be evidence about the double.

The eight reads: the search, the listing and its summary, the two counts of
excluded photos, the map, and the counts per device and per folder.
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
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.image_presence import ImagePresence, MissingImages
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.listing import ListingOrder
from app.domain.value_objects.position import BoundingBox
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
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
SCAN = datetime.datetime(2026, 10, 31, 9, 0, tzinfo=datetime.UTC)
LATER_SCAN = SCAN + datetime.timedelta(days=3)
OTHER_DEVICE = make_device(
    "\\\\?\\Volume{0000feed-0000-0000-0000-000000000000}\\", label="OTHER"
)
VECTOR = EmbeddingVector([0.1] * EMBEDDING_DIMENSION)
WHOLE_WORLD = BoundingBox(-90.0, -180.0, 90.0, 180.0)
YEAR_2018 = DateRange(
    start=datetime.datetime(2018, 1, 1), end=datetime.datetime(2019, 1, 1)
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


def image(
    path: str,
    device_id: DeviceId = TEST_DEVICE_ID,
    dated: bool = True,
    placed: bool = True,
) -> Image:
    name = path.rsplit("/", 1)[-1].removesuffix(".jpg")
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=device_id,
        relative_path=ImagePath(path),
        filename=name,
        extension="jpg",
        captured_at=datetime.datetime(2018, 6, 1, 10, 0) if dated else None,
        capture_source=CaptureSource.EXIF_ORIGINAL if dated else CaptureSource.UNKNOWN,
        latitude=-25.43 if placed else None,
        longitude=-49.27 if placed else None,
        position_source=PositionSource.EXIF_GPS if placed else PositionSource.UNKNOWN,
    )


def indexed(
    repository: ImageRepository, picture: Image, thumbnail: str | None = None
) -> Image:
    repository.save_indexed(
        IndexingRecord(
            image=picture,
            embedding=VECTOR,
            file_size=1024,
            file_modified_at=MODIFIED_AT,
            content_hash="a" * 64,
            thumbnail_path=thumbnail,
        )
    )
    return picture


def mark(
    repository: ImageRepository, *pictures: Image, at: datetime.datetime = SCAN
) -> None:
    repository.mark_presence([picture.id for picture in pictures], [], at)


# ----------------------------------------------------------------------
# The eight reads hide a marked row


class TestTheReadsHideAMarkedRow:
    """Each read, with one visible row and one marked beside it."""

    @pytest.fixture()
    def pair(self, repository: ImageRepository) -> tuple[Image, Image]:
        here = indexed(repository, image("2018/here.jpg"))
        gone = indexed(repository, image("2018/gone.jpg"))
        mark(repository, gone)
        return here, gone

    def test_the_search(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        here, _ = pair
        hits = repository.search_similar(VECTOR, 10)
        assert [hit.image.id for hit in hits] == [here.id]

    def test_the_listing(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        here, _ = pair
        listed = repository.list_by_filters(
            SearchFilters(), ListingOrder.NEWEST, 10, None
        )
        assert [picture.id for picture in listed] == [here.id]

    def test_the_summary_of_the_listing(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        assert repository.summarize_listing(SearchFilters()).total == 1

    def test_the_count_of_unknown_dates(self, repository: ImageRepository) -> None:
        indexed(repository, image("undated/here.jpg", dated=False))
        gone = indexed(repository, image("undated/gone.jpg", dated=False))
        mark(repository, gone)

        filters = SearchFilters(captured_between=YEAR_2018)
        assert repository.count_unknown_capture_date(filters) == 1

    def test_the_count_of_unknown_positions(self, repository: ImageRepository) -> None:
        indexed(repository, image("nowhere/here.jpg", placed=False))
        gone = indexed(repository, image("nowhere/gone.jpg", placed=False))
        mark(repository, gone)

        assert repository.count_unknown_position(SearchFilters()) == 1

    def test_the_map(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        (cell,) = repository.aggregate_positions(SearchFilters(), WHOLE_WORLD, 2, None)
        assert cell.count == 1

    def test_the_count_per_device(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        assert repository.count_by_device() == {TEST_DEVICE_ID: 1}

    def test_the_count_per_folder(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        assert repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope()) == {
            "2018": 1
        }

    def test_a_row_alone_and_marked_leaves_the_device_out(
        self, repository: ImageRepository
    ) -> None:
        """Absent, not zero: the rule of `count_by_device()` for an empty device."""
        gone = indexed(repository, image("2018/gone.jpg"))
        mark(repository, gone)

        assert repository.count_by_device() == {}
        assert repository.count_by_path_prefixes(TEST_DEVICE_ID, JobScope()) == {}

    def test_the_photo_itself_still_answers(
        self, repository: ImageRepository, pair: tuple[Image, Image]
    ) -> None:
        """`GET /images/{id}` keeps answering an old link (section 5.8)."""
        _, gone = pair
        assert repository.get(gone.id) is not None
        assert repository.get_embedding(gone.id) == VECTOR


# ----------------------------------------------------------------------
# Marking and unmarking


def test_the_presence_of_every_row_of_the_device(repository: ImageRepository) -> None:
    here = indexed(repository, image("a/here.jpg"))
    gone = indexed(repository, image("a/gone.jpg"))
    indexed(repository, image("a/theirs.jpg", OTHER_DEVICE.id))
    mark(repository, gone)

    presence = repository.list_presence(TEST_DEVICE_ID)

    assert sorted(presence, key=lambda row: row.relative_path) == [
        ImagePresence(image_id=gone.id, relative_path="a/gone.jpg", missing=True),
        ImagePresence(image_id=here.id, relative_path="a/here.jpg", missing=False),
    ]


def test_a_row_already_marked_keeps_its_date(repository: ImageRepository) -> None:
    gone = indexed(repository, image("a/gone.jpg"))
    mark(repository, gone, at=SCAN)

    mark(repository, gone, at=LATER_SCAN)

    assert repository.count_missing_by_device() == {
        TEST_DEVICE_ID: MissingImages(count=1, since=SCAN)
    }


def test_unmarking_brings_the_row_back(repository: ImageRepository) -> None:
    back = indexed(repository, image("a/back.jpg"))
    mark(repository, back)

    repository.mark_presence([], [back.id], LATER_SCAN)

    assert repository.count_missing_by_device() == {}
    assert [hit.image.id for hit in repository.search_similar(VECTOR, 10)] == [back.id]


def test_marking_and_unmarking_in_one_call(repository: ImageRepository) -> None:
    old = indexed(repository, image("old/x.jpg"))
    new = indexed(repository, image("new/x.jpg"))
    mark(repository, old)

    repository.mark_presence([new.id], [old.id], LATER_SCAN)

    presence = repository.list_presence(TEST_DEVICE_ID)
    assert {row.image_id: row.missing for row in presence} == {
        old.id: False,
        new.id: True,
    }


def test_an_id_both_missing_and_present_is_refused(
    repository: ImageRepository,
) -> None:
    picture = indexed(repository, image("a/x.jpg"))

    with pytest.raises(ValueError):
        repository.mark_presence([picture.id], [picture.id], SCAN)

    assert repository.count_missing_by_device() == {}


def test_an_id_with_no_row_is_skipped(repository: ImageRepository) -> None:
    repository.mark_presence([ImageId(uuid.uuid4())], [ImageId(uuid.uuid4())], SCAN)

    assert repository.count_missing_by_device() == {}


def test_save_indexed_unmarks(repository: ImageRepository) -> None:
    """A photo that was just indexed exists (section 5.1)."""
    picture = indexed(repository, image("a/x.jpg"))
    mark(repository, picture)

    indexed(repository, picture)

    assert repository.count_missing_by_device() == {}
    (row,) = repository.list_presence(TEST_DEVICE_ID)
    assert row.missing is False


def test_save_indexed_many_unmarks(repository: ImageRepository) -> None:
    picture = indexed(repository, image("a/x.jpg"))
    mark(repository, picture)

    repository.save_indexed_many(
        [
            IndexingRecord(
                image=picture,
                embedding=VECTOR,
                file_size=2048,
                file_modified_at=MODIFIED_AT,
                content_hash="b" * 64,
            )
        ]
    )

    assert repository.count_missing_by_device() == {}


def test_the_count_of_missing_per_device_with_the_oldest_mark(
    repository: ImageRepository,
) -> None:
    first = indexed(repository, image("a/1.jpg"))
    second = indexed(repository, image("a/2.jpg"))
    theirs = indexed(repository, image("a/3.jpg", OTHER_DEVICE.id))
    indexed(repository, image("a/here.jpg"))
    mark(repository, second, at=LATER_SCAN)
    mark(repository, first, theirs, at=SCAN)

    assert repository.count_missing_by_device() == {
        TEST_DEVICE_ID: MissingImages(count=2, since=SCAN),
        OTHER_DEVICE.id: MissingImages(count=1, since=SCAN),
    }


# ----------------------------------------------------------------------
# The purge


def test_the_purge_deletes_only_the_marked_rows_of_the_device(
    repository: ImageRepository,
) -> None:
    here = indexed(repository, image("a/here.jpg"), thumbnail="aa/here.jpg")
    gone = indexed(repository, image("a/gone.jpg"), thumbnail="bb/gone.jpg")
    bare = indexed(repository, image("a/bare.jpg"))
    theirs = indexed(repository, image("a/theirs.jpg", OTHER_DEVICE.id), "cc/t.jpg")
    mark(repository, gone, bare, theirs)

    purged = repository.purge_missing(TEST_DEVICE_ID)

    assert purged.count == 2
    assert purged.thumbnail_paths == ("bb/gone.jpg",)
    assert repository.get(gone.id) is None
    assert repository.get(bare.id) is None
    assert repository.get(here.id) is not None
    assert repository.get(theirs.id) is not None
    assert repository.count_missing_by_device() == {
        OTHER_DEVICE.id: MissingImages(count=1, since=SCAN)
    }


def test_a_purge_with_nothing_marked_deletes_nothing(
    repository: ImageRepository,
) -> None:
    here = indexed(repository, image("a/here.jpg"))

    purged = repository.purge_missing(TEST_DEVICE_ID)

    assert (purged.count, purged.thumbnail_paths) == (0, ())
    assert repository.get(here.id) is not None


def test_a_purged_photo_found_again_is_a_new_row(repository: ImageRepository) -> None:
    """The file came back after the purge: the next scan indexes it again."""
    picture = indexed(repository, image("a/x.jpg"))
    mark(repository, picture)
    repository.purge_missing(TEST_DEVICE_ID)

    indexed(repository, picture)

    assert repository.count_by_device() == {TEST_DEVICE_ID: 1}
    assert repository.count_missing_by_device() == {}
