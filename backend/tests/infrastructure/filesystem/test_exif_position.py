"""RFC-032 section 4: the position, read from files Pillow actually wrote.

Every fixture is a real JPEG whose GPS block sits in the **GPS IFD**, pointed at
by tag 0x8825 of IFD0 -- where DJI cameras write it -- and never a dictionary
assembled in IFD0. RFC-032 section 4.1 is the reason: a reader looking in the
wrong IFD passes every test built the same wrong way, and returns `unknown`
for 95% of the collection. The two cases that put the tags in the wrong IFD on
purpose are here to pin that.

The `0, 0` placeholder, half a position, a missing hemisphere and a zero
denominator are each their own case, because each is a separate line of the
reader that a "simplification" could fold into another.
"""

from __future__ import annotations

import datetime
import errno
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.position import Position, PositionReading
from app.domain.value_objects.position_source import PositionSource
from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
from app.infrastructure.filesystem import exif_capture_date
from app.infrastructure.filesystem.exif_capture_date import (
    DATE_TIME_ORIGINAL,
    EXIF_IFD_POINTER,
    GPS_IFD_POINTER,
    GPS_LATITUDE,
    GPS_LATITUDE_REF,
    GPS_LONGITUDE,
    GPS_LONGITUDE_REF,
    ExifFacts,
    is_null_island,
    parse_gps_coordinate,
    read_exif_facts,
)
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)

SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)
SHOT_TEXT = "2018:07:14 15:32:05"

DJI_0013_LATITUDE = (IFDRational(26, 1), IFDRational(19, 1), IFDRational(170615, 10000))
DJI_0013_LONGITUDE = (
    IFDRational(48, 1),
    IFDRational(48, 1),
    IFDRational(587052, 10000),
)
"""`DJI_0013.JPG` of the pilot, as degrees/minutes/seconds: -26.321406, -48.816307."""

UNKNOWN = PositionReading.unknown()


def gps_block(
    latitude: object = DJI_0013_LATITUDE,
    latitude_ref: object = "S",
    longitude: object = DJI_0013_LONGITUDE,
    longitude_ref: object = "W",
) -> dict[int, object]:
    """A GPS IFD; a value of `None` leaves that tag out."""
    tags = {
        GPS_LATITUDE_REF: latitude_ref,
        GPS_LATITUDE: latitude,
        GPS_LONGITUDE_REF: longitude_ref,
        GPS_LONGITUDE: longitude,
    }
    return {tag: value for tag, value in tags.items() if value is not None}


def write_image(
    path: Path,
    gps: dict[int, object] | None = None,
    date: str | None = None,
    misplaced_in: int | None = None,
) -> Path:
    """Write a real JPEG; `misplaced_in` puts the GPS tags in IFD0 or the sub-IFD."""
    exif = Image.Exif()
    if date is not None:
        exif.get_ifd(EXIF_IFD_POINTER)[DATE_TIME_ORIGINAL] = date
    if gps:
        if misplaced_in is None:
            exif.get_ifd(GPS_IFD_POINTER).update(gps)
        elif misplaced_in == 0:
            for tag, value in gps.items():
                exif[tag] = value
        else:
            exif.get_ifd(misplaced_in).update(gps)
    kwargs = {"exif": exif.tobytes()} if (gps or date) else {}
    Image.new("RGB", (16, 16), "gray").save(path, "JPEG", **kwargs)
    return path


def position_of(path: Path) -> PositionReading | None:
    return read_exif_facts(path).position


class TestAValidPosition:
    def test_the_pilot_coordinate_is_read_with_its_sign(self, tmp_path: Path) -> None:
        """South and west are negative: dropping the reference moves Parana north."""
        reading = position_of(write_image(tmp_path / "DJI_0013.JPG", gps_block()))

        assert reading is not None
        assert reading.source is PositionSource.EXIF_GPS
        assert reading.latitude == pytest.approx(-26.321406, abs=1e-6)
        assert reading.longitude == pytest.approx(-48.816307, abs=1e-6)

    def test_north_and_east_are_positive(self, tmp_path: Path) -> None:
        reading = position_of(
            write_image(
                tmp_path / "north.jpg", gps_block(latitude_ref="N", longitude_ref="E")
            )
        )

        assert reading is not None
        assert reading.latitude == pytest.approx(26.321406, abs=1e-6)
        assert reading.longitude == pytest.approx(48.816307, abs=1e-6)

    def test_degrees_minutes_and_seconds_all_count(self, tmp_path: Path) -> None:
        reading = position_of(
            write_image(
                tmp_path / "dms.jpg",
                gps_block(
                    latitude=(
                        IFDRational(10, 1),
                        IFDRational(30, 1),
                        IFDRational(36, 1),
                    ),
                    latitude_ref="N",
                    longitude=(
                        IFDRational(20, 1),
                        IFDRational(15, 1),
                        IFDRational(0, 1),
                    ),
                    longitude_ref="E",
                ),
            )
        )

        assert reading == PositionReading(
            Position(10.51, 20.25), PositionSource.EXIF_GPS
        )

    def test_a_decimal_degree_written_in_the_first_slot_reads_the_same(
        self, tmp_path: Path
    ) -> None:
        """Some writers put the whole value in degrees and zeros after it."""
        reading = position_of(
            write_image(
                tmp_path / "decimal.jpg",
                gps_block(
                    latitude=(
                        IFDRational(263214, 10000),
                        IFDRational(0, 1),
                        IFDRational(0, 1),
                    )
                ),
            )
        )

        assert reading is not None
        assert reading.latitude == pytest.approx(-26.3214)

    @pytest.mark.parametrize("reference", ["S\x00", b"S", b"S\x00", " s "])
    def test_the_reference_survives_nul_padding_bytes_and_case(
        self, reference: object
    ) -> None:
        assert parse_gps_coordinate(
            DJI_0013_LATITUDE, reference, {"N": 1, "S": -1}
        ) == pytest.approx(-26.321406, abs=1e-6)


class TestTheGpsIfdIsWhereItIsRead:
    """RFC-032 section 4.1: one IFD over from the date, and nowhere else."""

    def test_gps_tags_in_ifd0_are_not_found(self, tmp_path: Path) -> None:
        path = write_image(tmp_path / "ifd0.jpg", gps_block(), misplaced_in=0)

        assert position_of(path) == UNKNOWN

    def test_gps_tags_in_the_exif_sub_ifd_are_not_found(self, tmp_path: Path) -> None:
        path = write_image(
            tmp_path / "sub-ifd.jpg", gps_block(), misplaced_in=EXIF_IFD_POINTER
        )

        assert position_of(path) == UNKNOWN


class TestWhatIsNotAPosition:
    """RFC-032 section 4.3, one row of its table per case."""

    def test_no_gps_ifd_at_all_is_unknown(self, tmp_path: Path) -> None:
        assert (
            position_of(write_image(tmp_path / "phone.jpg", date=SHOT_TEXT)) == UNKNOWN
        )

    @pytest.mark.parametrize(
        "missing",
        [
            {"latitude": None},
            {"longitude": None},
            {"latitude": None, "latitude_ref": None},
            {"longitude": None, "longitude_ref": None},
        ],
        ids=["no-latitude", "no-longitude", "no-latitude-pair", "no-longitude-pair"],
    )
    def test_half_a_position_is_unknown_never_stored(
        self, tmp_path: Path, missing: dict[str, None]
    ) -> None:
        path = write_image(tmp_path / "half.jpg", gps_block(**missing))

        assert position_of(path) == UNKNOWN

    @pytest.mark.parametrize(
        "references",
        [
            {"latitude_ref": None},
            {"longitude_ref": None},
            {"latitude_ref": "X"},
            {"longitude_ref": "N"},
            {"latitude_ref": "E"},
        ],
        ids=[
            "no-lat-ref",
            "no-lon-ref",
            "nonsense",
            "lat-letter-on-lon",
            "lon-letter-on-lat",
        ],
    )
    def test_a_hemisphere_is_never_guessed(
        self, tmp_path: Path, references: dict[str, object]
    ) -> None:
        path = write_image(tmp_path / "ref.jpg", gps_block(**references))

        assert position_of(path) == UNKNOWN

    def test_a_zero_denominator_is_unknown(self, tmp_path: Path) -> None:
        broken = (IFDRational(26, 1), IFDRational(19, 0), IFDRational(17, 1))
        path = write_image(tmp_path / "zero-den.jpg", gps_block(latitude=broken))

        assert position_of(path) == UNKNOWN

    @pytest.mark.parametrize(
        "raw",
        [
            (26.0, 19.0),
            (26.0, 19.0, 17.0, 0.0),
            (float("nan"), 19.0, 17.0),
            (-26.0, 19.0, 17.0),
            "26 19 17",
            None,
        ],
        ids=["two", "four", "nan", "negative", "text", "none"],
    )
    def test_a_malformed_triple_is_none(self, raw: object) -> None:
        assert parse_gps_coordinate(raw, "S", {"N": 1, "S": -1}) is None

    def test_the_null_island_placeholder_is_unknown(self, tmp_path: Path) -> None:
        """`0, 0` is "no satellite fix", recognised by name, not by range."""
        zero = (IFDRational(0, 1), IFDRational(0, 1), IFDRational(0, 1))
        path = write_image(
            tmp_path / "no-fix.jpg",
            gps_block(
                latitude=zero, longitude=zero, latitude_ref="N", longitude_ref="E"
            ),
        )

        assert position_of(path) == UNKNOWN

    def test_zeros_with_noise_are_still_the_placeholder(self, tmp_path: Path) -> None:
        """A partial fix writes zeros with noise: within ~11 m of `0, 0`."""
        noise = (IFDRational(0, 1), IFDRational(0, 1), IFDRational(2, 10))
        path = write_image(
            tmp_path / "noisy.jpg", gps_block(latitude=noise, longitude=noise)
        )

        assert position_of(path) == UNKNOWN

    def test_only_both_near_zero_is_the_placeholder(self) -> None:
        """A real point on the equator, or on the prime meridian, is not one."""
        assert is_null_island(0.00005, -0.00005)
        assert not is_null_island(0.00005, 9.0)
        assert not is_null_island(-26.3, 0.00005)

    @pytest.mark.parametrize("axis", ["latitude", "longitude"])
    def test_a_coordinate_off_the_planet_is_unknown(
        self, tmp_path: Path, axis: str
    ) -> None:
        huge = (IFDRational(200, 1), IFDRational(0, 1), IFDRational(0, 1))
        path = write_image(tmp_path / "huge.jpg", gps_block(**{axis: huge}))

        assert position_of(path) == UNKNOWN


class TestTheTwoFactsAreIndependent:
    """RFC-032 section 4.2: one open, two facts, three outcomes per fact."""

    def test_a_date_without_gps(self, tmp_path: Path) -> None:
        facts = read_exif_facts(write_image(tmp_path / "phone.jpg", date=SHOT_TEXT))

        assert facts == ExifFacts(
            capture_date=CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL),
            position=UNKNOWN,
        )

    def test_gps_without_a_date(self, tmp_path: Path) -> None:
        facts = read_exif_facts(write_image(tmp_path / "gps-only.jpg", gps_block()))

        assert facts.capture_date == CaptureDate.unknown()
        assert facts.position is not None
        assert facts.position.source is PositionSource.EXIF_GPS

    def test_both_from_one_file(self, tmp_path: Path) -> None:
        facts = read_exif_facts(
            write_image(tmp_path / "dji.jpg", gps_block(), date=SHOT_TEXT)
        )

        assert facts.capture_date == CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)
        assert facts.position is not None
        assert facts.position.source is PositionSource.EXIF_GPS

    def test_a_damaged_gps_ifd_leaves_the_date_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Damage to one IFD cannot take the other fact with it."""
        path = write_image(tmp_path / "dji.jpg", gps_block(), date=SHOT_TEXT)
        real_get_ifd = Image.Exif.get_ifd

        def broken_gps(self: Image.Exif, tag: int) -> object:
            if tag == GPS_IFD_POINTER:
                raise ValueError("corrupt GPS IFD")
            return real_get_ifd(self, tag)

        monkeypatch.setattr(Image.Exif, "get_ifd", broken_gps)

        facts = read_exif_facts(path)

        assert facts.capture_date == CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)
        assert facts.position == UNKNOWN

    def test_a_disk_error_while_reading_the_gps_ifd_examines_neither(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An errno is the disk, not the bytes: unexamined, so a later scan retries."""
        path = write_image(tmp_path / "dji.jpg", gps_block(), date=SHOT_TEXT)
        real_get_ifd = Image.Exif.get_ifd

        def failing(self: Image.Exif, tag: int) -> object:
            if tag == GPS_IFD_POINTER:
                raise OSError(errno.EIO, "I/O error")
            return real_get_ifd(self, tag)

        monkeypatch.setattr(Image.Exif, "get_ifd", failing)

        assert read_exif_facts(path) == ExifFacts(capture_date=None, position=None)

    def test_a_file_that_cannot_be_opened_examines_neither(
        self, tmp_path: Path
    ) -> None:
        assert read_exif_facts(tmp_path / "vanished.jpg") == ExifFacts(None, None)

    def test_a_file_that_is_not_an_image_is_unknown_for_both(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "notes.jpg"
        path.write_bytes(b"this is not a jpeg")

        assert read_exif_facts(path) == ExifFacts(CaptureDate.unknown(), UNKNOWN)

    def test_a_fact_switched_off_is_not_examined(self, tmp_path: Path) -> None:
        """Off means "nobody looked", never "nothing there"."""
        path = write_image(tmp_path / "dji.jpg", gps_block(), date=SHOT_TEXT)

        assert read_exif_facts(path, position=False).position is None
        assert read_exif_facts(path, capture_date=False).capture_date is None


class _OpenSpy:
    """Counts `Image.open()` calls made by the EXIF reader."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real_open = Image.open

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return real_open(*args, **kwargs)

        monkeypatch.setattr(Image, "open", spy)


class TestOneOpenPerFile:
    """RFC-032 section 4.2 is a cost claim, and cost claims here are checked."""

    @pytest.fixture()
    def folder(self, tmp_path: Path) -> Path:
        for index in range(5):
            write_image(tmp_path / f"DJI_{index:04d}.JPG", gps_block(), date=SHOT_TEXT)
        return tmp_path

    def test_a_scan_with_both_facts_opens_each_file_once(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _OpenSpy(monkeypatch)

        discovered = list(
            FilesystemImageProvider(folder, SUPPORTED_IMAGE_EXTENSIONS).discover()
        )

        assert len(discovered) == 5
        assert spy.calls == 5
        assert all(item.capture_date is not None for item in discovered)
        assert all(item.position is not None for item in discovered)

    def test_one_fact_alone_still_costs_one_open(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _OpenSpy(monkeypatch)

        discovered = list(
            FilesystemImageProvider(
                folder, SUPPORTED_IMAGE_EXTENSIONS, extract_capture_date=False
            ).discover()
        )

        assert spy.calls == 5
        assert all(item.capture_date is None for item in discovered)
        assert all(item.position is not None for item in discovered)

    def test_both_switched_off_opens_nothing(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _OpenSpy(monkeypatch)

        discovered = list(
            FilesystemImageProvider(
                folder,
                SUPPORTED_IMAGE_EXTENSIONS,
                extract_capture_date=False,
                extract_gps=False,
            ).discover()
        )

        assert spy.calls == 0
        assert all(item.position is None for item in discovered)

    def test_the_date_reader_alone_is_still_one_open(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`read_capture_date()` remains the date's reader, at the same cost."""
        spy = _OpenSpy(monkeypatch)

        exif_capture_date.read_capture_date(folder / "DJI_0000.JPG")

        assert spy.calls == 1
