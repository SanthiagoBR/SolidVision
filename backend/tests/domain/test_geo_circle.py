"""`GeoCircle`, the haversine it is judged by, and `SearchFilters` with three fields."""

from __future__ import annotations

import datetime
import math
import uuid

import pytest

from app.domain.exceptions import InvalidGeoCircleError
from app.domain.services.haversine import EARTH_RADIUS_M, haversine_m
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.position import Position
from app.domain.value_objects.search_filters import SearchFilters

FARM = Position(-26.321406, -48.816307)


def destination(start: Position, bearing_deg: float, distance_m: float) -> Position:
    """The point `distance_m` metres from `start` along `bearing_deg`, on the sphere."""
    angular = distance_m / EARTH_RADIUS_M
    bearing = math.radians(bearing_deg)
    latitude = math.radians(start.latitude)
    longitude = math.radians(start.longitude)
    end_latitude = math.asin(
        math.sin(latitude) * math.cos(angular)
        + math.cos(latitude) * math.sin(angular) * math.cos(bearing)
    )
    end_longitude = longitude + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(latitude),
        math.cos(angular) - math.sin(latitude) * math.sin(end_latitude),
    )
    return Position(
        math.degrees(end_latitude),
        (math.degrees(end_longitude) + 540) % 360 - 180,
    )


class TestHaversine:
    def test_a_point_is_zero_metres_from_itself(self) -> None:
        assert haversine_m(FARM, FARM) == 0.0

    def test_it_is_symmetric(self) -> None:
        other = Position(-26.551, -49.133)

        assert haversine_m(FARM, other) == pytest.approx(haversine_m(other, FARM))

    def test_one_degree_of_latitude_is_about_111_km(self) -> None:
        assert haversine_m(Position(0, 0), Position(1, 0)) == pytest.approx(
            111_195, rel=1e-4
        )

    def test_a_degree_of_longitude_shrinks_with_latitude(self) -> None:
        at_equator = haversine_m(Position(0, 0), Position(0, 1))
        at_farm = haversine_m(Position(-26.3, 0), Position(-26.3, 1))

        assert at_farm == pytest.approx(
            at_equator * math.cos(math.radians(26.3)), rel=1e-3
        )

    def test_short_distances_stay_exact(self) -> None:
        """Why haversine and not the law of cosines: metres, not noise."""
        for metres in (1, 10, 87):
            assert haversine_m(FARM, destination(FARM, 33, metres)) == pytest.approx(
                metres, rel=1e-6
            )

    def test_antipodes_do_not_raise(self) -> None:
        assert haversine_m(Position(0, 0), Position(0, 180)) == pytest.approx(
            math.pi * EARTH_RADIUS_M
        )


class TestConstruction:
    def test_a_point_and_a_radius_in_metres(self) -> None:
        circle = GeoCircle(FARM, 2000)

        assert circle.center == FARM
        assert circle.radius_m == 2000

    @pytest.mark.parametrize("radius", [0, -1, -0.0001])
    def test_a_radius_that_is_not_positive_is_refused(self, radius: float) -> None:
        """Unlike `DateRange(x, x)`: no map click draws a circle of radius zero."""
        with pytest.raises(InvalidGeoCircleError, match="positive"):
            GeoCircle(FARM, radius)

    @pytest.mark.parametrize("radius", [math.nan, math.inf])
    def test_a_radius_that_is_not_finite_is_refused(self, radius: float) -> None:
        with pytest.raises(InvalidGeoCircleError):
            GeoCircle(FARM, radius)

    def test_a_radius_that_is_not_a_number_is_refused(self) -> None:
        with pytest.raises(InvalidGeoCircleError):
            GeoCircle(FARM, "2 km")  # type: ignore[arg-type]

    def test_the_centre_must_be_a_position(self) -> None:
        with pytest.raises(InvalidGeoCircleError):
            GeoCircle((-26.3, -48.8), 2000)  # type: ignore[arg-type]

    def test_there_is_no_upper_bound(self) -> None:
        """ "Anywhere in Brazil" is a request; a large radius changes selectivity."""
        circle = GeoCircle(FARM, 5_000_000)

        assert circle.contains(Position(-3.7, -38.5))

    def test_there_is_no_minimum_here(self) -> None:
        """`MIN_RADIUS_M` is Application policy, not a property of circles."""
        assert GeoCircle(FARM, 1).radius_m == 1


class TestContains:
    def test_the_edge_is_included_and_just_past_it_is_not(self) -> None:
        circle = GeoCircle(FARM, 2000)

        assert circle.contains(destination(FARM, 90, 1999.9))
        assert not circle.contains(destination(FARM, 90, 2000.1))

    def test_the_answer_is_a_circle_not_a_square(self) -> None:
        """A box alone would return corners 1.41 times the radius away (§9)."""
        circle = GeoCircle(FARM, 2000)
        corner = destination(FARM, 45, 2000 * math.sqrt(2) * 0.99)

        assert not circle.contains(corner)
        box = circle.bounding_box()
        assert box is not None
        assert box.contains(corner)


class TestBoundingBox:
    @pytest.mark.parametrize("radius", [50, 2000, 100_000, 1_000_000])
    @pytest.mark.parametrize("latitude", [-80, -26.32, 0, 45, 70])
    def test_the_box_contains_the_whole_circle(
        self, latitude: float, radius: float
    ) -> None:
        """Disposable, but never wrong: a box that clipped the circle hides photos."""
        circle = GeoCircle(Position(latitude, 10.0), radius)
        box = circle.bounding_box()
        assert box is not None

        for bearing in range(0, 360, 5):
            edge = destination(circle.center, bearing, radius * 0.999999)
            assert box.contains(edge), (bearing, edge)

    def test_the_box_is_not_absurdly_larger_than_the_circle(self) -> None:
        circle = GeoCircle(FARM, 2000)
        box = circle.bounding_box()
        assert box is not None

        height = haversine_m(
            Position(box.min_latitude, FARM.longitude),
            Position(box.max_latitude, FARM.longitude),
        )
        assert height == pytest.approx(4000, rel=1e-6)

    def test_a_circle_over_a_pole_has_no_box(self) -> None:
        """Every longitude is inside it; `min <= lon <= max` cannot say so."""
        assert GeoCircle(Position(89.99, 0), 5000).bounding_box() is None
        assert GeoCircle(Position(-89.99, 0), 5000).bounding_box() is None

    def test_a_circle_across_the_antimeridian_has_no_box(self) -> None:
        """Its box would wrap, and an inverted box matches nothing."""
        assert GeoCircle(Position(0, 179.99), 5000).bounding_box() is None
        assert GeoCircle(Position(0, -179.99), 5000).bounding_box() is None

    def test_a_circle_in_brazil_always_has_one(self) -> None:
        """The case that never fires here is still the one written down (§6)."""
        assert GeoCircle(FARM, 300).bounding_box() is not None
        assert GeoCircle(FARM, 3_000_000).bounding_box() is not None


class TestSearchFiltersWithThreeFields:
    def test_a_circle_alone_is_not_empty(self) -> None:
        """The regression `is_empty()` exists to prevent, for the third time.

        Had RFC-032 added `taken_within` without updating it, a circle-only
        filter would read as empty, every repository would skip its clauses,
        and "near here" would return photos from everywhere.
        """
        assert not SearchFilters(taken_within=GeoCircle(FARM, 2000)).is_empty()

    def test_the_default_is_still_empty(self) -> None:
        filters = SearchFilters()

        assert filters.is_empty()
        assert filters.taken_within is None

    def test_all_three_fields_can_be_set_together(self) -> None:
        device = DeviceId(uuid.uuid4())
        year = DateRange(datetime.datetime(2018, 1, 1), datetime.datetime(2019, 1, 1))
        circle = GeoCircle(FARM, 2000)

        filters = SearchFilters(
            device_ids=frozenset({device}), captured_between=year, taken_within=circle
        )

        assert filters.taken_within == circle
        assert not filters.is_empty()

    def test_filters_with_a_circle_stay_hashable(self) -> None:
        circle = GeoCircle(FARM, 2000)

        assert hash(SearchFilters(taken_within=circle)) == hash(
            SearchFilters(taken_within=GeoCircle(FARM, 2000))
        )
