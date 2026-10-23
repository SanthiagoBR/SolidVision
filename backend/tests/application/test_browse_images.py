"""Unit tests for `BrowseImagesUseCase` (RFC-044 section 5.4).

The fake repository only. The order, the cursor and the summary are
repository properties, held for all three implementations by
`test_listing_contract.py`; what is under test here is what the Application
layer owns: the request policy it shares with search, the cursor read back by
its id, the `limit + 1` that makes `next_after` exact, and the summary that is
the first page's alone.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.application.use_cases.browse_images import (
    BrowseImagesUseCase,
    ImageListing,
)
from app.application.use_cases.search_images import MAX_SEARCH_LIMIT
from app.domain.entities.image import Image
from app.domain.exceptions import (
    ImageNotFoundError,
    InvalidGeoCircleError,
    InvalidSearchLimitError,
)
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.listing import ListingOrder, ListingSummary
from app.domain.value_objects.position import Position
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

DEFAULT_LIMIT = 3
FARM = Position(-26.3214, -48.8163)
NEAR_FARM = SearchFilters(taken_within=GeoCircle(FARM, 1000))
EMBEDDING = EmbeddingVector([0.1] * 512)


def place(
    repository: FakeImageRepository,
    name: str,
    captured_at: datetime.datetime | None = None,
    where: Position | None = FARM,
) -> Image:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"obra/{name}.jpg"),
        filename=name,
        extension="jpg",
        captured_at=captured_at,
        capture_source=CaptureSource.EXIF_ORIGINAL if captured_at else None,
        latitude=where.latitude if where else None,
        longitude=where.longitude if where else None,
        position_source=PositionSource.EXIF_GPS if where else None,
    )
    repository.seed_embedding(image, EMBEDDING)
    return image


def build(
    repository: FakeImageRepository | None = None, min_radius_m: float = 0.0
) -> tuple[BrowseImagesUseCase, FakeImageRepository]:
    repository = repository if repository is not None else FakeImageRepository()
    use_case = BrowseImagesUseCase(
        repository=repository, default_limit=DEFAULT_LIMIT, min_radius_m=min_radius_m
    )
    return use_case, repository


def site(repository: FakeImageRepository, count: int) -> list[Image]:
    """`count` photos of the site, one a month, newest first."""
    images = [
        place(repository, f"m{month:02}", datetime.datetime(2025, month, 1))
        for month in range(1, count + 1)
    ]
    return list(reversed(images))


class TestTheRequestPolicy:
    """The search's, by the same functions, before any read (section 5.4)."""

    @pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1])
    def test_an_unusable_page_is_refused_before_reading(self, limit: int) -> None:
        use_case, repository = build()

        with pytest.raises(InvalidSearchLimitError):
            use_case.execute(NEAR_FARM, limit=limit, after=ImageId(uuid.uuid4()))

        assert repository.list_by_filters_calls == []
        assert repository.summarize_listing_calls == []

    def test_a_radius_below_the_floor_is_refused_before_reading(self) -> None:
        use_case, repository = build(min_radius_m=300)
        small = SearchFilters(taken_within=GeoCircle(FARM, 100))

        with pytest.raises(InvalidGeoCircleError):
            use_case.execute(small)

        assert repository.list_by_filters_calls == []

    def test_a_default_limit_out_of_range_fails_at_construction(self) -> None:
        with pytest.raises(InvalidSearchLimitError):
            BrowseImagesUseCase(FakeImageRepository(), default_limit=0)

    def test_the_default_limit_is_the_injected_one(self) -> None:
        use_case, repository = build()
        site(repository, 5)

        listing = use_case.execute(NEAR_FARM)

        assert listing.limit == DEFAULT_LIMIT
        assert len(listing.images) == DEFAULT_LIMIT


class TestTheCursor:
    def test_an_unknown_after_is_image_not_found(self) -> None:
        use_case, repository = build()
        site(repository, 2)

        with pytest.raises(ImageNotFoundError):
            use_case.execute(NEAR_FARM, after=ImageId(uuid.uuid4()))

        assert repository.list_by_filters_calls == []

    def test_after_is_read_back_and_handed_to_the_repository(self) -> None:
        use_case, repository = build()
        images = site(repository, 5)

        listing = use_case.execute(NEAR_FARM, limit=2, after=images[1].id)

        assert listing.images == images[2:4]
        (call,) = repository.list_by_filters_calls
        assert call == (NEAR_FARM, ListingOrder.NEWEST, 3, images[1])

    def test_the_order_is_passed_through(self) -> None:
        use_case, repository = build()
        images = site(repository, 3)

        listing = use_case.execute(NEAR_FARM, ListingOrder.OLDEST)

        assert listing.order is ListingOrder.OLDEST
        assert listing.images == list(reversed(images))


class TestNextAfter:
    def test_limit_plus_one_rows_means_there_is_a_next_page(self) -> None:
        use_case, repository = build()
        images = site(repository, 4)

        listing = use_case.execute(NEAR_FARM, limit=3)

        assert listing.images == images[:3]
        assert listing.next_after == images[2].id
        assert repository.list_by_filters_calls[0][2] == 4

    def test_exactly_limit_rows_means_the_list_is_over(self) -> None:
        """Not "fewer than asked": a full last page has no next one either."""
        use_case, repository = build()
        images = site(repository, 3)

        listing = use_case.execute(NEAR_FARM, limit=3)

        assert listing.images == images
        assert listing.next_after is None

    def test_following_next_after_walks_the_whole_list(self) -> None:
        use_case, repository = build()
        images = site(repository, 7)
        place(repository, "sem-data")

        seen: list[Image] = []
        page: ImageListing = use_case.execute(NEAR_FARM, limit=3)
        seen.extend(page.images)
        while page.next_after is not None:
            page = use_case.execute(NEAR_FARM, limit=3, after=page.next_after)
            seen.extend(page.images)

        assert seen[:7] == images
        assert len(seen) == 8
        assert seen[7].captured_at is None

    def test_an_empty_list_is_one_empty_page(self) -> None:
        use_case, _ = build()

        listing = use_case.execute(NEAR_FARM)

        assert listing.images == []
        assert listing.next_after is None


class TestTheOverview:
    def test_the_first_page_carries_it(self) -> None:
        use_case, repository = build()
        place(repository, "a", datetime.datetime(2015, 1, 3))
        place(repository, "b", datetime.datetime(2026, 10, 2))
        place(repository, "c")

        overview = use_case.execute(NEAR_FARM).overview

        assert overview is not None
        assert overview.listing == ListingSummary(
            total=3,
            undated=1,
            oldest=datetime.datetime(2015, 1, 3),
            newest=datetime.datetime(2026, 10, 2),
        )

    def test_a_later_page_does_not_pay_for_it(self) -> None:
        use_case, repository = build()
        images = site(repository, 5)

        listing = use_case.execute(NEAR_FARM, after=images[0].id)

        assert listing.overview is None
        assert repository.summarize_listing_calls == []
        assert repository.count_unknown_capture_date_calls == []
        assert repository.count_unknown_position_calls == []

    def test_without_a_range_or_a_circle_the_counts_are_none_and_not_asked(
        self,
    ) -> None:
        use_case, repository = build()
        place(repository, "a", datetime.datetime(2018, 1, 1))

        overview = use_case.execute(SearchFilters()).overview

        assert overview is not None
        assert overview.excluded_unknown_date is None
        assert overview.excluded_unknown_position is None
        assert repository.count_unknown_capture_date_calls == []
        assert repository.count_unknown_position_calls == []

    def test_with_a_range_and_a_circle_the_searchs_counts(self) -> None:
        use_case, repository = build()
        place(repository, "dated", datetime.datetime(2018, 6, 1))
        place(repository, "undated-here")
        place(repository, "nowhere", datetime.datetime(2018, 6, 1), where=None)
        filters = SearchFilters(
            captured_between=DateRange(
                datetime.datetime(2018, 1, 1), datetime.datetime(2019, 1, 1)
            ),
            taken_within=GeoCircle(FARM, 1000),
        )

        overview = use_case.execute(filters).overview

        assert overview is not None
        assert overview.listing.total == 1
        assert overview.listing.undated == 0
        assert overview.excluded_unknown_date == 1
        assert overview.excluded_unknown_position == 1

    def test_the_whole_collection_is_a_valid_question(self) -> None:
        """No filters at all: the API answers it, the interface does not ask."""
        use_case, repository = build()
        place(repository, "here", datetime.datetime(2018, 1, 1))
        elsewhere = place(repository, "elsewhere", datetime.datetime(2019, 1, 1), None)

        listing = use_case.execute(SearchFilters())

        assert listing.images[0] == elsewhere
        assert listing.overview is not None
        assert listing.overview.listing.total == 2
