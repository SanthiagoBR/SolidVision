"""RFC-032's circle, counts and map grouping: one contract, three implementations.

Run against `PostgresImageRepository`, `InMemoryImageRepository` and
`tests.application.fakes.FakeImageRepository`, for the reason
`test_search_similar_contract.py` gives: a double that filtered or grouped
slightly differently from PostgreSQL would make every Application test above
it evidence about the double.

Distances are built with a destination formula on the same sphere the
haversine uses, and every point is placed clearly inside or clearly outside a
circle -- metres, not nanometres. Exactly on the edge, the database's `sin`
and Python's may round one unit apart, and no contract can pin that.

Every assertion holds **at the scale this file runs at**, where PostgreSQL
answers from a sequential scan and is exact. At a scale where the planner
keeps the approximate HNSW index a circle can return fewer than `limit` rows;
that is `experiments/rfc-032-geolocation/planner_check.py`'s to measure.
"""

from __future__ import annotations

import datetime
import math
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID
from tests.infrastructure.persistence.test_search_similar_contract import (
    SECOND_DEVICE_ID,
    SECOND_VOLUME_IDENTITY,
    one_hot,
)

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.haversine import EARTH_RADIUS_M
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.position import BoundingBox, Position, PositionCell
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
from app.infrastructure.database.models.image_model import ImageModel
from app.infrastructure.persistence.engine import EngineInstance
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)

FARM = Position(-26.321406, -48.816307)
NEAR_FARM = GeoCircle(FARM, 2000)
YEAR_2018 = DateRange(datetime.datetime(2018, 1, 1), datetime.datetime(2019, 1, 1))
REGION = BoundingBox(-27.0, -50.0, -26.0, -48.0)


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


def destination(start: Position, bearing_deg: float, distance_m: float) -> Position:
    """The point `distance_m` metres from `start` along a bearing, on the sphere."""
    angular = distance_m / EARTH_RADIUS_M
    bearing = math.radians(bearing_deg)
    latitude = math.radians(start.latitude)
    end_latitude = math.asin(
        math.sin(latitude) * math.cos(angular)
        + math.cos(latitude) * math.sin(angular) * math.cos(bearing)
    )
    end_longitude = math.radians(start.longitude) + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(latitude),
        math.cos(angular) - math.sin(latitude) * math.sin(end_latitude),
    )
    wrapped = (math.degrees(end_longitude) + 540) % 360 - 180
    return Position(math.degrees(end_latitude), wrapped)


def index_image(
    repository: ImageRepository,
    embedding: EmbeddingVector | None = None,
    at: Position | None = None,
    examined: bool = True,
    device_id: DeviceId | None = None,
    captured_at: datetime.datetime | None = None,
    with_embedding: bool = True,
) -> Image:
    """Persist one image; `at=None` is unknown, `examined=False` never examined."""
    unique = uuid.uuid4().hex
    source = None
    if examined:
        source = PositionSource.EXIF_GPS if at is not None else PositionSource.UNKNOWN
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=device_id or TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/position/{unique}.jpg"),
        filename=unique,
        extension="jpg",
        captured_at=captured_at,
        capture_source=CaptureSource.EXIF_ORIGINAL if captured_at else None,
        latitude=at.latitude if at else None,
        longitude=at.longitude if at else None,
        position_source=source,
    )
    if with_embedding:
        repository.save_indexed(
            IndexingRecord(
                image=image,
                embedding=embedding or one_hot(0),
                file_size=1,
                file_modified_at=None,
            )
        )
    else:
        repository.save(image)
    return image


def near(filters_circle: GeoCircle = NEAR_FARM, **others: object) -> SearchFilters:
    return SearchFilters(taken_within=filters_circle, **others)  # type: ignore[arg-type]


class TestTheCircle:
    """RFC-032 section 6: a point and a radius, judged by exact distance."""

    def test_inside_is_kept_and_outside_is_not(
        self, repository: ImageRepository
    ) -> None:
        inside = index_image(repository, at=destination(FARM, 30, 1500))
        index_image(repository, at=destination(FARM, 30, 2600))
        index_image(repository, at=Position(-26.551, -49.133))

        hits = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert [hit.image for hit in hits] == [inside]

    def test_it_is_a_circle_and_not_its_box(self, repository: ImageRepository) -> None:
        """The box is a pre-filter: its corners, 1.41 radii away, never match."""
        index_image(repository, at=destination(FARM, 45, 2000 * math.sqrt(2) * 0.98))
        index_image(repository, at=destination(FARM, 225, 2000 * 1.2))
        center = index_image(repository, at=FARM)

        hits = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert [hit.image for hit in hits] == [center]

    @pytest.mark.parametrize("bearing", [0, 90, 180, 270])
    def test_just_inside_the_edge_counts_in_every_direction(
        self, repository: ImageRepository, bearing: float
    ) -> None:
        kept = index_image(repository, at=destination(FARM, bearing, 1990))
        index_image(repository, at=destination(FARM, bearing, 2010))

        hits = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert [hit.image for hit in hits] == [kept]

    def test_an_unknown_position_never_matches(
        self, repository: ImageRepository
    ) -> None:
        """RFC-020's rule, named by RFC-032 sections 6 and 13.

        Both kinds of unknown -- never examined, and examined without a fix
        -- are excluded from a circle around the whole planet. In Python
        that takes an explicit `is None`; in SQL it is free.
        """
        placed = index_image(repository, at=FARM)
        index_image(repository, at=None, examined=False)
        index_image(repository, at=None, examined=True)

        everywhere = SearchFilters(taken_within=GeoCircle(Position(0, 0), 25_000_000))
        hits = repository.search_similar(one_hot(0), limit=10, filters=everywhere)

        assert [hit.image for hit in hits] == [placed]

    def test_a_circle_without_a_box_is_still_exact(
        self, repository: ImageRepository
    ) -> None:
        """Across the 180th meridian the pre-filter is dropped; the answer is not."""
        dateline = Position(-17.0, 179.99)
        circle = GeoCircle(dateline, 5000)
        assert circle.bounding_box() is None
        east = index_image(repository, at=destination(dateline, 90, 3000))
        west = index_image(repository, at=destination(dateline, 270, 3000))
        index_image(repository, at=destination(dateline, 90, 8000))

        hits = repository.search_similar(
            one_hot(0), limit=10, filters=SearchFilters(taken_within=circle)
        )

        assert {hit.image for hit in hits} == {east, west}

    def test_a_circle_does_not_resurrect_images_without_an_embedding(
        self, repository: ImageRepository
    ) -> None:
        indexed = index_image(repository, at=FARM)
        index_image(repository, at=FARM, with_embedding=False)

        hits = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert [hit.image for hit in hits] == [indexed]

    def test_ordering_and_scores_are_unchanged_within_the_circle(
        self, repository: ImageRepository
    ) -> None:
        """The circle restricts candidates; it never ranks by distance (§9)."""
        identical = index_image(repository, one_hot(0), at=destination(FARM, 0, 1900))
        orthogonal = index_image(repository, one_hot(1), at=FARM)
        opposite = index_image(repository, one_hot(0, sign=-1.0), at=FARM)
        index_image(repository, one_hot(0), at=Position(-26.9, -49.4))

        hits = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert [hit.image for hit in hits] == [identical, orthogonal, opposite]
        assert [hit.similarity for hit in hits] == [
            pytest.approx(1.0),
            pytest.approx(0.0),
            pytest.approx(-1.0),
        ]

    def test_limit_still_applies_inside_the_circle(
        self, repository: ImageRepository
    ) -> None:
        for axis in range(5):
            index_image(repository, one_hot(axis), at=FARM)

        assert len(repository.search_similar(one_hot(0), limit=2, filters=near())) == 2

    def test_hits_carry_their_position(self, repository: ImageRepository) -> None:
        index_image(repository, at=FARM)

        (hit,) = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert hit.image.latitude == FARM.latitude
        assert hit.image.longitude == FARM.longitude
        assert hit.image.position_source is PositionSource.EXIF_GPS

    def test_an_unfiltered_hit_reports_a_never_examined_row_as_none(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=None, examined=False)

        (hit,) = repository.search_similar(one_hot(0), limit=10)

        assert hit.image.position_reading is None


class TestTheCircleCombinesWithAnd:
    def test_circle_and_device(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        wanted = index_image(repository, at=FARM)
        index_image(repository, at=FARM, device_id=second_device)
        index_image(repository, at=Position(-26.9, -49.4))

        hits = repository.search_similar(
            one_hot(0),
            limit=10,
            filters=near(device_ids=frozenset({TEST_DEVICE_ID})),
        )

        assert [hit.image for hit in hits] == [wanted]

    def test_circle_and_date(self, repository: ImageRepository) -> None:
        wanted = index_image(
            repository, at=FARM, captured_at=datetime.datetime(2018, 7, 14)
        )
        index_image(repository, at=FARM, captured_at=datetime.datetime(2011, 7, 14))
        index_image(repository, at=FARM)
        index_image(
            repository,
            at=Position(-26.9, -49.4),
            captured_at=datetime.datetime(2018, 7, 14),
        )

        hits = repository.search_similar(
            one_hot(0), limit=10, filters=near(captured_between=YEAR_2018)
        )

        assert [hit.image for hit in hits] == [wanted]

    def test_a_circle_only_filter_does_not_restrict_devices_or_dates(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        here = index_image(repository, one_hot(0), at=FARM)
        elsewhere = index_image(
            repository, one_hot(1), at=FARM, device_id=second_device
        )
        undated = index_image(repository, one_hot(2), at=FARM)

        hits = repository.search_similar(one_hot(0), limit=10, filters=near())

        assert {hit.image for hit in hits} == {here, elsewhere, undated}


class TestUnknownPositionCount:
    """RFC-032 section 6.2: how many photos a circle hid for having no position."""

    def test_both_kinds_of_unknown_are_counted(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=None, examined=False)
        index_image(repository, at=None, examined=True)
        index_image(repository, at=FARM)
        index_image(repository, at=Position(-26.9, -49.4))

        assert repository.count_unknown_position(near()) == 2

    def test_images_without_an_embedding_are_not_counted(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=None, with_embedding=False)
        index_image(repository, at=None)

        assert repository.count_unknown_position(near()) == 1

    def test_the_device_and_date_clauses_still_apply(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        index_image(repository, at=None, captured_at=datetime.datetime(2018, 7, 1))
        index_image(repository, at=None, captured_at=datetime.datetime(2011, 7, 1))
        index_image(
            repository,
            at=None,
            device_id=second_device,
            captured_at=datetime.datetime(2018, 7, 1),
        )

        filters = near(
            device_ids=frozenset({TEST_DEVICE_ID}), captured_between=YEAR_2018
        )

        assert repository.count_unknown_position(filters) == 1

    def test_without_a_circle_it_still_counts_for_the_map(
        self, repository: ImageRepository
    ) -> None:
        """Not 0: the map asks this question with no circle (§7). The search's
        "do not ask without a circle" lives in the use case."""
        index_image(repository, at=None)
        index_image(repository, at=FARM)

        assert repository.count_unknown_position(SearchFilters()) == 1

    def test_the_count_is_not_bounded_by_the_search_limit(
        self, repository: ImageRepository
    ) -> None:
        for axis in range(7):
            index_image(repository, one_hot(axis), at=None)

        assert repository.search_similar(one_hot(0), limit=2, filters=near()) == []
        assert repository.count_unknown_position(near()) == 7


class TestTheDateCountRespectsTheCircle:
    """ "Every other clause" of RFC-028's count now includes the circle."""

    def test_an_undated_photo_outside_the_circle_was_not_hidden_by_the_date(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=FARM)
        index_image(repository, at=Position(-26.9, -49.4))

        filters = near(captured_between=YEAR_2018)

        assert repository.count_unknown_capture_date(filters) == 1


class TestAggregatePositions:
    """RFC-032 section 7: one grouping, the same cells in all three."""

    def test_cells_are_rounded_centres_with_counts(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=Position(-26.32141, -48.81631))
        index_image(repository, at=Position(-26.32139, -48.81629))
        index_image(repository, at=Position(-26.55102, -49.13301))

        cells = repository.aggregate_positions(SearchFilters(), REGION, 3, None)

        assert cells == [
            PositionCell(latitude=-26.551, longitude=-49.133, count=1),
            PositionCell(latitude=-26.321, longitude=-48.816, count=2),
        ]

    @pytest.mark.parametrize("precision", [0, 1, 2, 4, 5])
    def test_every_precision_groups_alike(
        self, repository: ImageRepository, precision: int
    ) -> None:
        for latitude, longitude in (
            (-26.32141, -48.81631),
            (-26.55102, -49.13301),
            (-26.22519, -49.41636),
            (-26.64800, -48.81620),
        ):
            index_image(repository, at=Position(latitude, longitude))

        cells = repository.aggregate_positions(SearchFilters(), REGION, precision, None)

        assert sum(cell.count for cell in cells) == 4
        for cell in cells:
            assert cell.latitude == round(cell.latitude, precision)

    def test_a_boundary_rounds_half_away_from_zero_on_the_decimal_digits(
        self, repository: ImageRepository
    ) -> None:
        """`round(x::numeric, p)`, not Python's binary `round()` (see the port)."""
        index_image(repository, at=Position(-26.0005, -48.0005))
        index_image(repository, at=Position(-26.2345, -48.2345))

        cells = repository.aggregate_positions(SearchFilters(), REGION, 3, None)

        assert [(cell.latitude, cell.longitude) for cell in cells] == [
            (-26.235, -48.235),
            (-26.001, -48.001),
        ]

    def test_the_area_is_edges_included(self, repository: ImageRepository) -> None:
        index_image(repository, at=Position(-27.0, -50.0))
        index_image(repository, at=Position(-26.0, -48.0))
        index_image(repository, at=Position(-25.99, -48.5))

        cells = repository.aggregate_positions(SearchFilters(), REGION, 2, None)

        assert sum(cell.count for cell in cells) == 2

    def test_only_searchable_placed_images_are_drawn(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=FARM)
        index_image(repository, at=FARM, with_embedding=False)
        index_image(repository, at=None)

        cells = repository.aggregate_positions(SearchFilters(), REGION, 3, None)

        assert [cell.count for cell in cells] == [1]

    def test_device_and_date_narrow_the_map(
        self, repository: ImageRepository, second_device: DeviceId
    ) -> None:
        index_image(repository, at=FARM, captured_at=datetime.datetime(2018, 7, 1))
        index_image(repository, at=FARM, captured_at=datetime.datetime(2011, 7, 1))
        index_image(
            repository,
            at=FARM,
            device_id=second_device,
            captured_at=datetime.datetime(2018, 7, 1),
        )

        cells = repository.aggregate_positions(
            SearchFilters(
                device_ids=frozenset({TEST_DEVICE_ID}), captured_between=YEAR_2018
            ),
            REGION,
            3,
            None,
        )

        assert [cell.count for cell in cells] == [1]

    def test_a_circle_in_the_filters_is_not_applied(
        self, repository: ImageRepository
    ) -> None:
        index_image(repository, at=FARM)
        index_image(repository, at=Position(-26.9, -49.4))

        cells = repository.aggregate_positions(
            SearchFilters(taken_within=GeoCircle(FARM, 300)), REGION, 3, None
        )

        assert sum(cell.count for cell in cells) == 2

    def test_the_limit_cuts_deterministically(
        self, repository: ImageRepository
    ) -> None:
        for index in range(5):
            index_image(repository, at=Position(-26.9 + index * 0.1, -49.0))

        cells = repository.aggregate_positions(SearchFilters(), REGION, 3, 3)

        assert [cell.latitude for cell in cells] == [-26.9, -26.8, -26.7]


class TestTheEmittedSql:
    """RFC-032 section 6, asserted on the compiled SQL rather than on results."""

    @staticmethod
    def compiled(filters: SearchFilters | None) -> str:
        base = select(ImageModel.id).where(ImageModel.embedding.is_not(None))
        statement = PostgresImageRepository._apply_filters(base, filters)
        return str(statement.compile(dialect=EngineInstance.dialect))

    def test_without_a_circle_the_unfiltered_query_is_unchanged(self) -> None:
        """The RFC-025 query, character for character, with or without filters."""
        unfiltered = str(
            select(ImageModel.id)
            .where(ImageModel.embedding.is_not(None))
            .compile(dialect=EngineInstance.dialect)
        )

        assert self.compiled(None) == unfiltered
        assert self.compiled(SearchFilters()) == unfiltered

    def test_a_date_or_device_filter_emits_no_position_clause(self) -> None:
        for filters in (
            SearchFilters(captured_between=YEAR_2018),
            SearchFilters(device_ids=frozenset({TEST_DEVICE_ID})),
        ):
            sql = self.compiled(filters)
            assert "latitude" not in sql
            assert "asin" not in sql

    def test_a_circle_emits_the_box_then_the_exact_distance(self) -> None:
        sql = self.compiled(near())

        assert "images.latitude BETWEEN" in sql
        assert "images.longitude BETWEEN" in sql
        assert "asin(CASE WHEN" in sql
        assert sql.index("BETWEEN") < sql.index("asin(")
        assert "captured_at" not in sql
        assert "device_id" not in sql
        assert "IS NULL" not in sql

    def test_a_circle_without_a_box_emits_only_the_distance(self) -> None:
        sql = self.compiled(
            SearchFilters(taken_within=GeoCircle(Position(89.99, 0), 5000))
        )

        assert "BETWEEN" not in sql
        assert "asin(CASE WHEN" in sql

    def test_the_distance_clamp_propagates_null(self) -> None:
        """`least()` ignores NULL in PostgreSQL; the clamp must not use it.

        With `least(1, sqrt(h))`, a row with no position measured pi * R
        from every centre, and a box-less circle that large matched it.
        """
        sql = self.compiled(
            SearchFilters(taken_within=GeoCircle(Position(0, 0), 25_000_000))
        ).lower()

        assert "least(" not in sql
        assert "greatest(" not in sql

    def test_the_distance_needs_no_extension(self) -> None:
        """No PostGIS, no earthdistance: plain functions over two columns (§5.1)."""
        sql = self.compiled(near()).lower()

        for foreign in ("st_", "earth_distance", "ll_to_earth", "geography", "<@>"):
            assert foreign not in sql
