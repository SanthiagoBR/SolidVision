from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import DateTime, Double, Enum, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position_source import PositionSource
from app.infrastructure.database.models.image_model import ImageModel


def test_from_domain_copies_fields() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.id == image.id.value
    assert model.device_id == image.device_id.value
    assert model.relative_path == str(image.relative_path)
    assert model.filename == image.filename
    assert model.extension == image.extension


def test_from_domain_leaves_embedding_unset() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.embedding is None


def test_from_domain_leaves_incremental_metadata_unset() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )

    model = ImageModel.from_domain(image)

    assert model.file_size is None
    assert model.file_modified_at is None


def test_to_domain_reconstructs_value_objects() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)

    reconstructed = model.to_domain()

    assert reconstructed.id == image.id
    assert reconstructed.device_id == image.device_id
    assert reconstructed.relative_path == image.relative_path
    assert reconstructed.filename == image.filename
    assert reconstructed.extension == image.extension
    assert isinstance(reconstructed.id, ImageId)
    assert isinstance(reconstructed.relative_path, ImagePath)
    assert isinstance(reconstructed.device_id, DeviceId)


def test_to_domain_ignores_unset_embedding() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    assert model.embedding is None

    reconstructed = model.to_domain()

    assert not hasattr(reconstructed, "embedding")
    assert reconstructed == image


def test_to_domain_ignores_populated_embedding() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    model.embedding = [0.1] * 512

    reconstructed = model.to_domain()

    assert not hasattr(reconstructed, "embedding")
    assert reconstructed == image


def test_to_domain_ignores_populated_incremental_metadata() -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/example.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    model.file_size = 1024
    model.file_modified_at = datetime.datetime.now(datetime.UTC)

    reconstructed = model.to_domain()

    assert not hasattr(reconstructed, "file_size")
    assert not hasattr(reconstructed, "file_modified_at")
    assert reconstructed == image


def test_round_trip_preserves_fields_individually() -> None:
    original = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath("images/roundtrip.png"),
        filename="roundtrip",
        extension="png",
    )

    reconstructed = ImageModel.from_domain(original).to_domain()

    assert reconstructed.id == original.id
    assert reconstructed.device_id == original.device_id
    assert reconstructed.relative_path == original.relative_path
    assert reconstructed.filename == original.filename
    assert reconstructed.extension == original.extension


def test_row_persists_with_incremental_metadata_left_none(
    db_session: Session,
) -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/{uuid.uuid4().hex}.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)

    db_session.add(model)
    db_session.commit()

    row = db_session.get(ImageModel, image.id.value)
    assert row is not None
    assert row.file_size is None
    assert row.file_modified_at is None


class TestCaptureDateColumns:
    """RFC-028 section 5: a camera-local time, stored without a zone."""

    SHOT = datetime.datetime(2018, 12, 31, 23, 30, 0)
    """Half an hour before midnight on New Year's Eve: the zone-sensitive case."""

    def test_captured_at_is_a_timestamp_without_time_zone(self) -> None:
        """The whole of RFC-028 section 5, as a schema assertion.

        `file_modified_at` beside it is `timezone=True`, and that is
        correct too. The two differ on purpose; this is what stops someone
        "fixing" the inconsistency.
        """
        column_type = ImageModel.__table__.c.captured_at.type
        neighbour_type = ImageModel.__table__.c.file_modified_at.type

        assert isinstance(column_type, DateTime)
        assert column_type.timezone is False
        assert isinstance(neighbour_type, DateTime)
        assert neighbour_type.timezone is True

    def test_both_columns_are_nullable(self) -> None:
        assert ImageModel.__table__.c.captured_at.nullable is True
        assert ImageModel.__table__.c.capture_source.nullable is True

    def test_capture_source_is_a_plain_string_not_a_database_enum(self) -> None:
        column_type = ImageModel.__table__.c.capture_source.type

        assert isinstance(column_type, String)
        assert not isinstance(column_type, Enum)

    def test_the_capture_date_round_trips_through_the_mapping(self) -> None:
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath("fotos/2018/DJI_0042.JPG"),
            filename="DJI_0042",
            extension="jpg",
            captured_at=self.SHOT,
            capture_source=CaptureSource.EXIF_ORIGINAL,
        )

        model = ImageModel.from_domain(image)
        reconstructed = model.to_domain()

        assert model.capture_source == "exif_original"
        assert reconstructed.captured_at == self.SHOT
        assert reconstructed.capture_source is CaptureSource.EXIF_ORIGINAL

    def test_an_unexamined_image_maps_to_null_not_unknown(self) -> None:
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath("images/example.png"),
            filename="example",
            extension="png",
        )

        model = ImageModel.from_domain(image)

        assert model.captured_at is None
        assert model.capture_source is None
        assert model.to_domain().capture_source is None

    def test_a_naive_capture_date_survives_postgresql_unchanged(
        self, db_session: Session
    ) -> None:
        """Written naive, read back naive, same wall-clock value.

        The failure this catches is silent: a `TIMESTAMPTZ` column would
        accept the naive value, interpret it in the session's zone, and
        hand back an aware datetime that is a different hour for anyone in
        a different zone.
        """
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath(f"fotos/{uuid.uuid4().hex}.jpg"),
            filename="nye",
            extension="jpg",
            captured_at=self.SHOT,
            capture_source=CaptureSource.EXIF_ORIGINAL,
        )
        db_session.add(ImageModel.from_domain(image))
        db_session.commit()
        db_session.expire_all()

        row = db_session.get(ImageModel, image.id.value)

        assert row is not None
        assert row.captured_at == self.SHOT
        assert row.captured_at is not None
        assert row.captured_at.tzinfo is None
        assert row.capture_source == "exif_original"

    def test_the_column_ignores_the_session_time_zone(
        self, db_session: Session
    ) -> None:
        """A `TIMESTAMP` is not shifted by `SET TIME ZONE`; a `TIMESTAMPTZ` would be."""
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath(f"fotos/{uuid.uuid4().hex}.jpg"),
            filename="nye",
            extension="jpg",
            captured_at=self.SHOT,
            capture_source=CaptureSource.EXIF_ORIGINAL,
        )
        db_session.add(ImageModel.from_domain(image))
        db_session.commit()

        db_session.execute(text("SET LOCAL TIME ZONE 'Pacific/Kiritimati'"))
        stored = db_session.execute(
            text("SELECT captured_at FROM images WHERE id = :id"),
            {"id": image.id.value},
        ).scalar_one()

        assert stored == self.SHOT


def test_negative_file_size_raises_check_constraint_violation(
    db_session: Session,
) -> None:
    image = Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/{uuid.uuid4().hex}.png"),
        filename="example",
        extension="png",
    )
    model = ImageModel.from_domain(image)
    model.file_size = -1

    db_session.add(model)
    with pytest.raises(IntegrityError):
        db_session.commit()


class TestThumbnailPathColumn:
    """RFC-030 section 7: a location in the app's cache, or NULL."""

    def test_the_column_is_a_nullable_plain_string(self) -> None:
        column = ImageModel.__table__.c.thumbnail_path

        assert column.nullable is True
        assert isinstance(column.type, String)

    def test_from_domain_leaves_it_unset(self) -> None:
        """`Image` carries no thumbnail; only an `IndexingRecord` writes one."""
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath("images/example.png"),
            filename="example",
            extension="png",
        )

        assert ImageModel.from_domain(image).thumbnail_path is None


class TestPositionColumns:
    """RFC-032 section 5: two doubles, a plain string, and three CHECKs."""

    def test_latitude_and_longitude_are_nullable_doubles(self) -> None:
        for name in ("latitude", "longitude"):
            column = ImageModel.__table__.c[name]
            assert column.nullable is True
            assert isinstance(column.type, Double)

    def test_position_source_is_a_plain_string_not_a_database_enum(self) -> None:
        column = ImageModel.__table__.c.position_source

        assert column.nullable is True
        assert isinstance(column.type, String)
        assert not isinstance(column.type, Enum)

    def test_the_position_round_trips_through_the_mapping(self) -> None:
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath("fotos/DJI_0013.JPG"),
            filename="DJI_0013",
            extension="jpg",
            latitude=-26.321406,
            longitude=-48.816307,
            position_source=PositionSource.EXIF_GPS,
        )

        model = ImageModel.from_domain(image)
        reconstructed = model.to_domain()

        assert model.position_source == "exif_gps"
        assert reconstructed.position_reading == image.position_reading

    def test_an_unexamined_image_maps_to_null_not_unknown(self) -> None:
        image = Image(
            id=ImageId(uuid.uuid4()),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath("fotos/old.jpg"),
            filename="old",
            extension="jpg",
        )

        model = ImageModel.from_domain(image)

        assert model.position_source is None
        assert model.to_domain().position_source is None


class TestThePositionChecksHoldWithoutTheDomain:
    """The CHECKs are what no writer can go around: tested by direct INSERT.

    Each statement goes straight to the table, past `Image` and every
    validation in Python, because that is exactly the writer the CHECKs
    exist for -- a manual `UPDATE`, a careless backfill, a fixture built by
    hand (RFC-032 section 5).
    """

    @staticmethod
    def insert(session: Session, **position: object) -> None:
        columns = ", ".join(position)
        values = ", ".join(f":{name}" for name in position)
        session.execute(
            text(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                f"extension{', ' if position else ''}{columns}) VALUES (:id, "
                f":device_id, :relative_path, 'x', 'jpg'{', ' if position else ''}"
                f"{values})"
            ),
            {
                "id": uuid.uuid4(),
                "device_id": TEST_DEVICE_ID.value,
                "relative_path": f"fotos/{uuid.uuid4().hex}.jpg",
                **position,
            },
        )

    def test_a_full_position_is_accepted(self, db_session: Session) -> None:
        self.insert(
            db_session,
            latitude=-26.321406,
            longitude=-48.816307,
            position_source="exif_gps",
        )
        db_session.flush()

    @pytest.mark.parametrize(
        "half", [{"latitude": -26.3}, {"longitude": -48.8}], ids=["lat", "lon"]
    )
    def test_half_a_position_is_refused(
        self, db_session: Session, half: dict[str, float]
    ) -> None:
        with pytest.raises(IntegrityError, match="ck_images_position_pairing"):
            self.insert(db_session, **half)

    @pytest.mark.parametrize(
        ("latitude", "longitude", "constraint"),
        [
            (90.0001, 0.0, "ck_images_latitude_range"),
            (-91.0, 0.0, "ck_images_latitude_range"),
            (0.0, 180.0001, "ck_images_longitude_range"),
            (0.0, -181.0, "ck_images_longitude_range"),
        ],
    )
    def test_a_coordinate_off_the_planet_is_refused(
        self,
        db_session: Session,
        latitude: float,
        longitude: float,
        constraint: str,
    ) -> None:
        with pytest.raises(IntegrityError, match=constraint):
            self.insert(db_session, latitude=latitude, longitude=longitude)

    def test_the_planet_edges_are_accepted(self, db_session: Session) -> None:
        self.insert(db_session, latitude=-90.0, longitude=180.0)
        self.insert(db_session, latitude=90.0, longitude=-180.0)
        db_session.flush()
