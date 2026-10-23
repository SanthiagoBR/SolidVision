"""RFC-044's listing by capture date: one contract, three implementations.

`list_by_filters()` and `summarize_listing()` against
`PostgresImageRepository`, `InMemoryImageRepository` and
`tests.application.fakes.FakeImageRepository`, for the reason
`test_search_similar_contract.py` gives: a double that ordered or paged
slightly differently from PostgreSQL would make every Application test above
it evidence about the double. The order is where the three could disagree
without anyone noticing -- `NULLS LAST`, a collation, the byte order of a
UUID -- so each part of the key has its own test.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator

import pytest
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID
from tests.infrastructure.persistence.test_position_filter_contract import (
    FARM,
    destination,
)
from tests.infrastructure.persistence.test_search_similar_contract import (
    SECOND_DEVICE_ID,
    SECOND_VOLUME_IDENTITY,
    one_hot,
)

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.listing import ListingOrder, ListingSummary
from app.domain.value_objects.position import Position
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)

NEWEST = ListingOrder.NEWEST
OLDEST = ListingOrder.OLDEST
EVERYTHING = SearchFilters()


def at(year: int, month: int = 1, day: int = 1, hour: int = 12) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, 0)


@pytest.fixture(params=["postgres", "in_memory", "fake"])
def repository(request: pytest.FixtureRequest) -> Iterator[ImageRepository]:
    """Each implementation in turn, with an empty table for the database's round."""
    if request.param == "postgres":
        yield PostgresImageRepository(request.getfixturevalue("empty_db_session"))
    elif request.param == "in_memory":
        yield InMemoryImageRepository()
    else:
        yield FakeImageRepository()


@pytest.fixture()
def second_device(request: pytest.FixtureRequest) -> DeviceId:
    """A second disk; a real row only where the foreign key needs one."""
    if "postgres" in request.node.callspec.id:
        now = datetime.datetime.now(tz=datetime.UTC)
        PostgresDeviceRepository(request.getfixturevalue("empty_db_session")).save(
            Device(
                id=SECOND_DEVICE_ID,
                volume_identity=SECOND_VOLUME_IDENTITY,
                label="SECOND-DEVICE",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    return SECOND_DEVICE_ID


def photo(
    repository: ImageRepository,
    path: str,
    captured_at: datetime.datetime | None = None,
    where: Position | None = None,
    device_id: DeviceId | None = None,
    with_embedding: bool = True,
) -> Image:
    """Persist one image at `path`; no `captured_at` is an examined, undated file."""
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=device_id or TEST_DEVICE_ID,
        relative_path=ImagePath(path),
        filename=path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
        extension="jpg",
        captured_at=captured_at,
        capture_source=(
            CaptureSource.EXIF_ORIGINAL if captured_at else CaptureSource.UNKNOWN
        ),
        latitude=where.latitude if where else None,
        longitude=where.longitude if where else None,
        position_source=PositionSource.EXIF_GPS if where else PositionSource.UNKNOWN,
    )
    if with_embedding:
        repository.save_indexed(
            IndexingRecord(
                image=image, embedding=one_hot(0), file_size=1, file_modified_at=None
            )
        )
    else:
        repository.save(image)
    return image


def listed(
    repository: ImageRepository,
    order: ListingOrder = NEWEST,
    filters: SearchFilters = EVERYTHING,
    limit: int = 100,
    after: Image | None = None,
) -> list[Image]:
    return repository.list_by_filters(filters, order, limit, after)


def paged(
    repository: ImageRepository,
    order: ListingOrder,
    size: int,
    filters: SearchFilters = EVERYTHING,
) -> list[list[Image]]:
    """Every page, each one starting after the last image of the page before."""
    pages: list[list[Image]] = []
    after: Image | None = None
    while True:
        page = repository.list_by_filters(filters, order, size, after)
        if not page:
            return pages
        pages.append(page)
        after = page[-1]


class TestTheOrder:
    """RFC-044 section 5.2: a total order, the date first."""

    def test_newest_runs_the_date_backwards_and_oldest_forwards(
        self, repository: ImageRepository
    ) -> None:
        middle = photo(repository, "obra/b.jpg", at(2019))
        first = photo(repository, "obra/c.jpg", at(2018))
        last = photo(repository, "obra/a.jpg", at(2020))

        assert listed(repository, NEWEST) == [last, middle, first]
        assert listed(repository, OLDEST) == [first, middle, last]

    def test_a_microsecond_is_a_different_date(
        self, repository: ImageRepository
    ) -> None:
        later = photo(repository, "a.jpg", datetime.datetime(2018, 1, 1, 0, 0, 0, 1))
        earlier = photo(repository, "b.jpg", datetime.datetime(2018, 1, 1))

        assert listed(repository, NEWEST) == [later, earlier]
        assert listed(repository, OLDEST) == [earlier, later]

    @pytest.mark.parametrize("order", [NEWEST, OLDEST])
    def test_the_undated_come_after_every_dated_one_in_folder_order(
        self, repository: ImageRepository, order: ListingOrder
    ) -> None:
        """Section 3.2: no undated photo between two dated ones, in either direction."""
        undated_b = photo(repository, "pasta/b.jpg")
        dated_old = photo(repository, "z/old.jpg", at(2015))
        undated_a = photo(repository, "pasta/a.jpg")
        dated_new = photo(repository, "z/new.jpg", at(2026))

        dated = [dated_new, dated_old] if order is NEWEST else [dated_old, dated_new]
        assert listed(repository, order) == [*dated, undated_a, undated_b]

    @pytest.mark.parametrize("order", [NEWEST, OLDEST])
    def test_the_same_second_is_in_the_cameras_numbering_both_ways(
        self, repository: ImageRepository, order: ListingOrder
    ) -> None:
        """A burst: the path breaks the tie, ascending in both directions."""
        same = at(2018, 7, 14)
        third = photo(repository, "voo/DJI_0044.JPG", same)
        first = photo(repository, "voo/DJI_0042.JPG", same)
        second = photo(repository, "voo/DJI_0043.JPG", same)

        assert listed(repository, order) == [first, second, third]

    def test_paths_compare_by_code_point(self, repository: ImageRepository) -> None:
        """`B` before `a`, and `Z` before `é`: the `C` collation, as Python's `str`.

        A linguistic collation would put `a` first, and the in-memory doubles
        would then disagree with the database about every mixed-case folder.
        """
        lower = photo(repository, "a.jpg")
        accented = photo(repository, "é.jpg")
        upper = photo(repository, "B.jpg")
        last_ascii = photo(repository, "Z.jpg")

        assert listed(repository) == [upper, last_ascii, lower, accented]

    @pytest.mark.parametrize("order", [NEWEST, OLDEST])
    def test_the_same_path_on_two_disks_is_ordered_by_id(
        self,
        repository: ImageRepository,
        second_device: DeviceId,
        order: ListingOrder,
    ) -> None:
        """A copy: same path, same date, two ids -- the id makes the order total."""
        same = at(2018)
        copies = [
            photo(repository, "fotos/DJI_0042.JPG", same),
            photo(repository, "fotos/DJI_0042.JPG", same, device_id=second_device),
        ]

        expected = sorted(copies, key=lambda image: image.id.value.int)
        assert listed(repository, order) == expected

    def test_the_dated_part_reverses_and_ties_keep_their_order(
        self, repository: ImageRepository
    ) -> None:
        """Reversing the direction reverses the dates, not the photos of one date.

        Only the date changes direction (section 5.2): the dated part of one
        direction is the other's reversed exactly when no two photos share a
        date, and within a shared date both directions keep the camera's order.
        """
        burst = at(2019)
        one = photo(repository, "x/DJI_0001.JPG", burst)
        two = photo(repository, "x/DJI_0002.JPG", burst)
        alone_old = photo(repository, "y/a.jpg", at(2018))
        alone_new = photo(repository, "y/b.jpg", at(2020))
        photo(repository, "sem-data.jpg")

        newest = [image for image in listed(repository, NEWEST) if image.captured_at]
        oldest = [image for image in listed(repository, OLDEST) if image.captured_at]

        assert newest == [alone_new, one, two, alone_old]
        assert oldest == [alone_old, one, two, alone_new]
        assert [i.captured_at for i in newest] == [
            i.captured_at for i in reversed(oldest)
        ]


class TestTheCursor:
    """`after` is a position in the order: the page starts right past it."""

    @pytest.fixture()
    def collection(self, repository: ImageRepository) -> list[Image]:
        """Newest first: two dated, a tie, then two undated."""
        return [
            photo(repository, "a/new.jpg", at(2021)),
            photo(repository, "a/DJI_1.JPG", at(2020)),
            photo(repository, "a/DJI_2.JPG", at(2020)),
            photo(repository, "b/old.jpg", at(2019)),
            photo(repository, "c/sem-data-1.jpg"),
            photo(repository, "c/sem-data-2.jpg"),
        ]

    def test_after_a_dated_photo(
        self, repository: ImageRepository, collection: list[Image]
    ) -> None:
        assert listed(repository, after=collection[1]) == collection[2:]

    def test_after_the_last_dated_photo_come_the_undated(
        self, repository: ImageRepository, collection: list[Image]
    ) -> None:
        assert listed(repository, after=collection[3]) == collection[4:]

    def test_after_an_undated_photo(
        self, repository: ImageRepository, collection: list[Image]
    ) -> None:
        assert listed(repository, after=collection[4]) == collection[5:]

    def test_after_the_last_photo_there_is_nothing(
        self, repository: ImageRepository, collection: list[Image]
    ) -> None:
        assert listed(repository, after=collection[-1]) == []

    def test_after_counts_in_the_order_asked(
        self, repository: ImageRepository, collection: list[Image]
    ) -> None:
        oldest = [collection[3], collection[1], collection[2], collection[0]]

        assert listed(repository, OLDEST, after=collection[1]) == [
            collection[2],
            collection[0],
            *collection[4:],
        ]
        assert listed(repository, OLDEST)[:4] == oldest

    def test_the_cursor_need_not_satisfy_the_filters(
        self, repository: ImageRepository
    ) -> None:
        """A photo from elsewhere continues the list from where it would be."""
        circle = SearchFilters(taken_within=GeoCircle(FARM, 1000))
        here_new = photo(repository, "here/new.jpg", at(2021), where=FARM)
        here_old = photo(repository, "here/old.jpg", at(2018), where=FARM)
        far = photo(
            repository, "far/mid.jpg", at(2019), where=destination(FARM, 0, 50_000)
        )

        assert listed(repository, filters=circle, after=far) == [here_old]
        assert listed(repository, filters=circle) == [here_new, here_old]

    def test_a_cursor_without_an_embedding_is_still_a_position(
        self, repository: ImageRepository
    ) -> None:
        newer = photo(repository, "a.jpg", at(2021))
        older = photo(repository, "b.jpg", at(2019))
        unindexed = photo(repository, "c.jpg", at(2020), with_embedding=False)

        assert listed(repository) == [newer, older]
        assert listed(repository, after=unindexed) == [older]

    @pytest.mark.parametrize("order", [NEWEST, OLDEST])
    @pytest.mark.parametrize("size", [1, 2, 3, 7])
    def test_page_after_page_is_the_list_at_once(
        self,
        repository: ImageRepository,
        second_device: DeviceId,
        order: ListingOrder,
        size: int,
    ) -> None:
        """No photo repeated, none skipped, whatever the page size."""
        burst = at(2020, 3, 3)
        for index in range(4):
            photo(repository, f"voo/DJI_{index:04}.JPG", burst)
        photo(repository, "voo/DJI_0000.JPG", burst, device_id=second_device)
        for year in (2016, 2018, 2018, 2024):
            photo(repository, f"anos/{year}-{uuid.uuid4().hex[:6]}.jpg", at(year))
        for index in range(3):
            photo(repository, f"sem-data/{index}.jpg")

        whole = listed(repository, order)
        pages = paged(repository, order, size)

        flat = [image for page in pages for image in page]
        assert flat == whole
        assert len({image.id for image in flat}) == len(whole) == 12
        assert all(len(page) == size for page in pages[:-1])

    def test_the_limit_cuts_the_page(self, repository: ImageRepository) -> None:
        images = [
            photo(repository, f"{index}.jpg", at(2000 + index)) for index in range(5)
        ]

        assert listed(repository, OLDEST, limit=2) == images[:2]
        assert listed(repository, OLDEST, limit=2, after=images[1]) == images[2:4]


class TestTheUniverse:
    """The search's filters and the search's universe (RFC-044 section 2.6)."""

    def test_an_image_without_an_embedding_never_appears(
        self, repository: ImageRepository
    ) -> None:
        indexed = photo(repository, "a.jpg", at(2018))
        photo(repository, "b.jpg", at(2019), with_embedding=False)

        assert listed(repository) == [indexed]

    def test_a_date_range_never_matches_an_unknown_date(
        self, repository: ImageRepository
    ) -> None:
        inside = photo(repository, "a.jpg", at(2018, 6))
        photo(repository, "b.jpg", at(2019, 6))
        photo(repository, "c.jpg")

        year_2018 = SearchFilters(
            captured_between=DateRange(at(2018, hour=0), at(2019, hour=0))
        )
        assert listed(repository, filters=year_2018) == [inside]

    def test_a_circle_never_matches_an_unknown_position(
        self, repository: ImageRepository
    ) -> None:
        here = photo(repository, "a.jpg", at(2018), where=destination(FARM, 90, 400))
        here_undated = photo(repository, "b.jpg", where=FARM)
        photo(repository, "c.jpg", at(2019), where=destination(FARM, 90, 1500))
        photo(repository, "d.jpg", at(2020))

        circle = SearchFilters(taken_within=GeoCircle(FARM, 1000))
        assert listed(repository, filters=circle) == [here, here_undated]

    def test_the_device_set_narrows(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        mine = photo(repository, "a.jpg", at(2018))
        photo(repository, "a.jpg", at(2018), device_id=second_device)

        only_mine = SearchFilters(device_ids=frozenset({TEST_DEVICE_ID}))
        assert listed(repository, filters=only_mine) == [mine]

    def test_an_empty_table_lists_nothing(self, repository: ImageRepository) -> None:
        assert listed(repository) == []
        assert listed(repository, OLDEST) == []

    def test_every_image_carries_its_date_and_position(
        self, repository: ImageRepository
    ) -> None:
        stored = photo(repository, "obra/DJI_0042.JPG", at(2018, 7, 14), where=FARM)

        (image,) = listed(repository)

        assert image.captured_at == stored.captured_at
        assert image.capture_source is CaptureSource.EXIF_ORIGINAL
        assert (image.latitude, image.longitude) == (FARM.latitude, FARM.longitude)
        assert image.position_source is PositionSource.EXIF_GPS
        assert image.relative_path == stored.relative_path
        assert image.device_id == stored.device_id


class TestTheSummary:
    """RFC-044 section 5.3: the whole list at a glance."""

    def test_the_count_the_undated_and_the_span(
        self, repository: ImageRepository
    ) -> None:
        photo(repository, "a.jpg", at(2015, 1, 3))
        photo(repository, "b.jpg", at(2026, 10, 2))
        photo(repository, "c.jpg", at(2020))
        photo(repository, "d.jpg")
        photo(repository, "e.jpg")
        photo(repository, "f.jpg", at(2030), with_embedding=False)

        assert repository.summarize_listing(EVERYTHING) == ListingSummary(
            total=5,
            undated=2,
            oldest=at(2015, 1, 3),
            newest=at(2026, 10, 2),
        )

    def test_under_a_date_range_nothing_is_undated(
        self, repository: ImageRepository
    ) -> None:
        photo(repository, "a.jpg", at(2018, 3))
        photo(repository, "b.jpg", at(2018, 9))
        photo(repository, "c.jpg")

        year_2018 = SearchFilters(
            captured_between=DateRange(at(2018, hour=0), at(2019, hour=0))
        )
        assert repository.summarize_listing(year_2018) == ListingSummary(
            total=2, undated=0, oldest=at(2018, 3), newest=at(2018, 9)
        )

    def test_only_undated_photos_have_no_span(
        self, repository: ImageRepository
    ) -> None:
        photo(repository, "a.jpg")

        assert repository.summarize_listing(EVERYTHING) == ListingSummary(
            total=1, undated=1, oldest=None, newest=None
        )

    def test_an_empty_table(self, repository: ImageRepository) -> None:
        assert repository.summarize_listing(EVERYTHING) == ListingSummary(
            total=0, undated=0, oldest=None, newest=None
        )

    def test_the_summary_is_of_the_list(self, repository: ImageRepository) -> None:
        """The same universe as the pages: what they add up to, under a circle."""
        for year in (2018, 2019, 2020):
            photo(repository, f"{year}.jpg", at(year), where=FARM)
        photo(repository, "sem-data.jpg", where=FARM)
        photo(repository, "longe.jpg", at(2017), where=destination(FARM, 0, 9000))

        circle = SearchFilters(taken_within=GeoCircle(FARM, 1000))
        summary = repository.summarize_listing(circle)

        assert summary.total == len(listed(repository, filters=circle)) == 4
        assert (summary.undated, summary.oldest, summary.newest) == (
            1,
            at(2018),
            at(2020),
        )
