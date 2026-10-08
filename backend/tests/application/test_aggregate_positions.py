"""The map's use case (RFC-032 section 7), against the fake repository.

What it owns is policy, and each case pins one piece: the universe is the
filters' and never the circle's, the grid coarsens instead of truncating, the
precision applied is reported, and the photos with no position are counted
under the same filters.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.application.use_cases.aggregate_positions import (
    DEFAULT_MAP_PRECISION,
    MAX_MAP_PRECISION,
    AggregatePositionsUseCase,
)
from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position import BoundingBox, Position, PositionCell
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

REGION = BoundingBox(-27.0, -50.0, -26.0, -48.0)
EMBEDDING = EmbeddingVector([0.1] * 512)


def place(
    repository: FakeImageRepository,
    latitude: float | None,
    longitude: float | None,
    captured_at: datetime.datetime | None = None,
    embedded: bool = True,
) -> Image:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"fotos/{uuid.uuid4().hex}.jpg"),
        filename="photo",
        extension="jpg",
        captured_at=captured_at,
        capture_source=CaptureSource.EXIF_ORIGINAL if captured_at else None,
        latitude=latitude,
        longitude=longitude,
        position_source=(
            PositionSource.EXIF_GPS if latitude is not None else PositionSource.UNKNOWN
        ),
    )
    if embedded:
        repository.seed_embedding(image, EMBEDDING)
    else:
        repository.save(image)
    return image


def use_case(
    repository: FakeImageRepository, max_cells: int = 1000
) -> AggregatePositionsUseCase:
    return AggregatePositionsUseCase(repository=repository, max_cells=max_cells)


def test_cells_are_centres_and_counts() -> None:
    repository = FakeImageRepository()
    place(repository, -26.32141, -48.81631)
    place(repository, -26.32139, -48.81629)
    place(repository, -26.55102, -49.13301)

    answer = use_case(repository).execute(REGION)

    assert answer.precision_applied == DEFAULT_MAP_PRECISION == 3
    assert answer.cells == [
        PositionCell(latitude=-26.551, longitude=-49.133, count=1),
        PositionCell(latitude=-26.321, longitude=-48.816, count=2),
    ]


def test_photos_outside_the_area_are_not_drawn() -> None:
    repository = FakeImageRepository()
    place(repository, -26.5, -49.0)
    place(repository, -23.5, -46.6)

    answer = use_case(repository).execute(REGION)

    assert sum(cell.count for cell in answer.cells) == 1


def test_only_searchable_images_are_counted() -> None:
    """The universe is "what a search could return": images with an embedding."""
    repository = FakeImageRepository()
    place(repository, -26.5, -49.0)
    place(repository, -26.5, -49.0, embedded=False)

    answer = use_case(repository).execute(REGION)

    assert [cell.count for cell in answer.cells] == [1]


def test_the_date_filter_narrows_the_map_as_it_narrows_search() -> None:
    repository = FakeImageRepository()
    place(repository, -26.5, -49.0, captured_at=datetime.datetime(2018, 7, 1))
    place(repository, -26.6, -49.1, captured_at=datetime.datetime(2011, 7, 1))
    year = DateRange(datetime.datetime(2018, 1, 1), datetime.datetime(2019, 1, 1))

    answer = use_case(repository).execute(REGION, SearchFilters(captured_between=year))

    assert [(cell.latitude, cell.count) for cell in answer.cells] == [(-26.5, 1)]


def test_a_circle_in_the_filters_is_dropped_not_applied() -> None:
    """The viewport is the map's area; "near here" is search's question (§9)."""
    repository = FakeImageRepository()
    place(repository, -26.5, -49.0)
    place(repository, -26.9, -48.1)
    far_away = SearchFilters(taken_within=GeoCircle(Position(10.0, 10.0), 1000))

    answer = use_case(repository).execute(REGION, far_away)

    assert sum(cell.count for cell in answer.cells) == 2
    ((filters, _, _, _),) = repository.aggregate_positions_calls
    assert filters.taken_within is None


def test_the_photos_without_a_position_are_counted_under_the_same_filters() -> None:
    """Not restricted to the area: a photo with no coordinates is in no area."""
    repository = FakeImageRepository()
    place(repository, -26.5, -49.0)
    place(repository, None, None)
    place(repository, None, None)
    place(repository, None, None, embedded=False)

    answer = use_case(repository).execute(REGION)

    assert answer.excluded_unknown_position == 2


class TestTheCeiling:
    """Above `max_cells` the grid coarsens; the list is never truncated."""

    def _spread(self, repository: FakeImageRepository) -> None:
        """Twelve photos ~1 km apart: 12 cells at precision 3, fewer at 1."""
        for index in range(12):
            place(repository, -26.5 + index * 0.01, -49.0 + index * 0.01)

    def test_a_grid_that_fits_is_answered_at_the_requested_precision(self) -> None:
        repository = FakeImageRepository()
        self._spread(repository)

        answer = use_case(repository, max_cells=12).execute(REGION, precision=3)

        assert answer.precision_requested == answer.precision_applied == 3
        assert len(answer.cells) == 12

    def test_too_many_cells_coarsen_the_grid_and_say_so(self) -> None:
        repository = FakeImageRepository()
        self._spread(repository)

        answer = use_case(repository, max_cells=5).execute(REGION, precision=3)

        assert answer.precision_requested == 3
        assert answer.precision_applied < 3
        assert len(answer.cells) <= 5
        assert sum(cell.count for cell in answer.cells) == 12

    def test_each_attempt_asks_for_one_cell_more_than_the_ceiling(self) -> None:
        """N + 1 is how "more than N" is known without counting everything."""
        repository = FakeImageRepository()
        self._spread(repository)

        use_case(repository, max_cells=5).execute(REGION, precision=3)

        precisions = [call[2] for call in repository.aggregate_positions_calls]
        limits = [call[3] for call in repository.aggregate_positions_calls]
        assert precisions == sorted(precisions, reverse=True)
        assert precisions[0] == 3
        assert all(limit == 6 for limit in limits if limit is not None)

    def test_at_precision_zero_nothing_is_hidden_to_honour_the_ceiling(self) -> None:
        """Exceeding the ceiling there is the honest failure; dropping photos is not."""
        repository = FakeImageRepository()
        place(repository, -26.5, -49.0)
        place(repository, -23.5, -46.6)
        place(repository, -3.7, -38.5)
        brazil = BoundingBox(-34.0, -74.0, 6.0, -34.0)

        answer = use_case(repository, max_cells=1).execute(brazil, precision=0)

        assert answer.precision_applied == 0
        assert len(answer.cells) == 3
        ((_, _, _, limit),) = repository.aggregate_positions_calls
        assert limit is None

    def test_a_precision_finer_than_offered_is_served_at_the_finest(self) -> None:
        repository = FakeImageRepository()
        place(repository, -26.5, -49.0)

        answer = use_case(repository).execute(REGION, precision=9)

        assert answer.precision_requested == 9
        assert answer.precision_applied == MAX_MAP_PRECISION

    def test_a_negative_precision_is_a_caller_error(self) -> None:
        with pytest.raises(ValueError):
            use_case(FakeImageRepository()).execute(REGION, precision=-1)

    def test_a_ceiling_below_one_is_a_wiring_error(self) -> None:
        with pytest.raises(ValueError):
            AggregatePositionsUseCase(FakeImageRepository(), max_cells=0)


def test_cell_boundaries_round_as_postgresql_does() -> None:
    """Half away from zero on the decimal digits, not Python's binary `round()`.

    `round(-26.0005, 3)` in Python is -26.0 or -26.001 depending on the
    binary expansion; PostgreSQL's `round(x::numeric, 3)` is -26.001. The
    in-memory implementations must say the same, or a photo on a boundary
    lands in different cells in different implementations.
    """
    repository = FakeImageRepository()
    place(repository, -26.0005, -48.0005)
    place(repository, -26.2345, -48.2345)

    answer = use_case(repository).execute(REGION)

    assert [(cell.latitude, cell.longitude) for cell in answer.cells] == [
        (-26.235, -48.235),
        (-26.001, -48.001),
    ]
