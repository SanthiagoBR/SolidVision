"""`Position`, `PositionReading`, `BoundingBox` and the pairing rule (RFC-032)."""

from __future__ import annotations

import math
import uuid

import pytest
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.exceptions import InvalidBoundingBoxError, InvalidPositionError
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position import (
    BoundingBox,
    Position,
    PositionReading,
    validate_position_fields,
)
from app.domain.value_objects.position_source import PositionSource

FARM = Position(-26.321406, -48.816307)


class TestPosition:
    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [(90, 180), (-90, -180), (0, 0), (-26.321406, -48.816307)],
        ids=["north-east-corner", "south-west-corner", "null-island", "farm"],
    )
    def test_any_point_on_the_planet_is_valid(
        self, latitude: float, longitude: float
    ) -> None:
        """Including 0, 0: the placeholder is recognised where files are read."""
        position = Position(latitude, longitude)

        assert (position.latitude, position.longitude) == (latitude, longitude)

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [(90.0001, 0), (-91, 0), (0, 180.0001), (0, -181)],
    )
    def test_a_point_off_the_planet_is_refused(
        self, latitude: float, longitude: float
    ) -> None:
        with pytest.raises(InvalidPositionError, match="outside"):
            Position(latitude, longitude)

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_a_coordinate_that_is_not_finite_is_refused(self, value: float) -> None:
        with pytest.raises(InvalidPositionError, match="finite"):
            Position(value, 0)
        with pytest.raises(InvalidPositionError, match="finite"):
            Position(0, value)

    @pytest.mark.parametrize("value", [True, "10", None])
    def test_a_coordinate_that_is_not_a_number_is_refused(self, value: object) -> None:
        with pytest.raises(InvalidPositionError, match="number"):
            Position(value, 0)  # type: ignore[arg-type]

    def test_positions_compare_by_value(self) -> None:
        assert Position(-26.5, -48.8) == Position(-26.5, -48.8)
        assert hash(Position(-26.5, -48.8)) == hash(Position(-26.5, -48.8))


class TestPositionReading:
    def test_a_reading_from_the_gps_carries_its_point(self) -> None:
        reading = PositionReading(FARM, PositionSource.EXIF_GPS)

        assert reading.latitude == FARM.latitude
        assert reading.longitude == FARM.longitude

    def test_unknown_is_examined_with_no_point(self) -> None:
        reading = PositionReading.unknown()

        assert reading.source is PositionSource.UNKNOWN
        assert reading.position is None
        assert reading.latitude is None
        assert reading.longitude is None

    def test_a_point_claimed_as_unknown_is_refused(self) -> None:
        with pytest.raises(InvalidPositionError, match="unknown"):
            PositionReading(FARM, PositionSource.UNKNOWN)

    def test_a_gps_source_with_no_point_is_refused(self) -> None:
        with pytest.raises(InvalidPositionError):
            PositionReading(None, PositionSource.EXIF_GPS)

    def test_the_source_must_be_a_position_source(self) -> None:
        with pytest.raises(InvalidPositionError):
            PositionReading(FARM, "exif_gps")  # type: ignore[arg-type]


class TestPositionSource:
    def test_values_are_the_stored_strings(self) -> None:
        assert PositionSource.EXIF_GPS.value == "exif_gps"
        assert PositionSource.UNKNOWN.value == "unknown"

    def test_gps_outranks_unknown(self) -> None:
        """What `--force` reads to refuse a downgrade (RFC-032 section 8.1)."""
        assert PositionSource.EXIF_GPS.precedence > PositionSource.UNKNOWN.precedence

    def test_the_future_sources_are_named_but_not_members(self) -> None:
        """`manual` and `subject_estimated` wait for a writer (RFC-032 section 4.4)."""
        values = {source.value for source in PositionSource}

        assert values == {"exif_gps", "unknown"}

    def test_there_is_no_xmp_source(self) -> None:
        """Measured: the DJI XMP packet carries no position (RFC-032 section 2.3)."""
        assert not any("xmp" in source.value for source in PositionSource)


class TestValidatePositionFields:
    def test_never_examined_has_no_coordinates(self) -> None:
        validate_position_fields(None, None, None)

    @pytest.mark.parametrize(("latitude", "longitude"), [(-26.3, None), (None, -48.8)])
    def test_half_a_position_is_refused(
        self, latitude: float | None, longitude: float | None
    ) -> None:
        """RFC-032 section 4.3: never half a position, whatever the source."""
        for source in (None, PositionSource.EXIF_GPS, PositionSource.UNKNOWN):
            with pytest.raises(InvalidPositionError, match="both"):
                validate_position_fields(latitude, longitude, source)

    def test_coordinates_without_a_source_are_refused(self) -> None:
        with pytest.raises(InvalidPositionError, match="source"):
            validate_position_fields(-26.3, -48.8, None)

    def test_unknown_with_coordinates_is_refused(self) -> None:
        with pytest.raises(InvalidPositionError):
            validate_position_fields(-26.3, -48.8, PositionSource.UNKNOWN)


def image(**fields: object) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("fotos/DJI_0013.JPG"),
        filename="DJI_0013",
        extension="jpg",
        **fields,  # type: ignore[arg-type]
    )


class TestTheEntity:
    def test_an_image_defaults_to_never_examined(self) -> None:
        entity = image()

        assert entity.position is None
        assert entity.position_reading is None

    def test_an_image_with_a_gps_position(self) -> None:
        entity = image(
            latitude=FARM.latitude,
            longitude=FARM.longitude,
            position_source=PositionSource.EXIF_GPS,
        )

        assert entity.position == FARM
        assert entity.position_reading == PositionReading(FARM, PositionSource.EXIF_GPS)

    def test_an_examined_image_without_a_position(self) -> None:
        entity = image(position_source=PositionSource.UNKNOWN)

        assert entity.position is None
        assert entity.position_reading == PositionReading.unknown()

    def test_half_a_position_cannot_be_constructed(self) -> None:
        with pytest.raises(InvalidPositionError):
            image(latitude=-26.3, position_source=PositionSource.EXIF_GPS)

    def test_a_position_off_the_planet_cannot_be_constructed(self) -> None:
        with pytest.raises(InvalidPositionError):
            image(
                latitude=-126.3,
                longitude=-48.8,
                position_source=PositionSource.EXIF_GPS,
            )

    def test_the_position_is_not_part_of_equality(self) -> None:
        """Equality stays by id, like every other field."""
        placed = image(
            latitude=FARM.latitude,
            longitude=FARM.longitude,
            position_source=PositionSource.EXIF_GPS,
        )
        same_file = Image(
            id=placed.id,
            device_id=placed.device_id,
            relative_path=placed.relative_path,
            filename=placed.filename,
            extension=placed.extension,
        )

        assert placed == same_file
        assert hash(placed) == hash(same_file)


class TestBoundingBox:
    def test_corners_are_included(self) -> None:
        box = BoundingBox(-27, -50, -26, -48)

        assert box.contains(Position(-27, -50))
        assert box.contains(Position(-26, -48))
        assert box.contains(Position(-26.5, -49))
        assert not box.contains(Position(-25.999, -49))
        assert not box.contains(Position(-26.5, -47.999))

    def test_a_degenerate_box_is_legal_and_matches_only_its_line(self) -> None:
        box = BoundingBox(-26.5, -49, -26.5, -48)

        assert box.contains(Position(-26.5, -48.5))
        assert not box.contains(Position(-26.4, -48.5))

    def test_an_inverted_latitude_is_refused(self) -> None:
        with pytest.raises(InvalidBoundingBoxError, match="north"):
            BoundingBox(-26, -50, -27, -48)

    def test_an_inverted_longitude_names_the_antimeridian(self) -> None:
        """Swapped corners or a viewport across 180: neither is guessed (§7)."""
        with pytest.raises(InvalidBoundingBoxError, match="180th meridian"):
            BoundingBox(-27, 170, -26, -170)

    def test_a_corner_off_the_planet_is_refused(self) -> None:
        with pytest.raises(InvalidPositionError):
            BoundingBox(-91, -50, -26, -48)
