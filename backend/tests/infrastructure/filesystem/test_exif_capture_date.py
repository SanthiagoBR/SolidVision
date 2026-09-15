"""The RFC-028 section 4 fallback chain, against files Pillow actually wrote.

Every fixture is a real image file with a real EXIF block, written through
`Image.Exif` into the Exif sub-IFD where cameras put it. Hand-assembled
dictionaries would test the parsing and miss the one mistake that matters
most in practice -- reading `DateTimeOriginal` from IFD0, where it is not.
"""

from __future__ import annotations

import datetime
import errno
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.infrastructure.filesystem.exif_capture_date import (
    DATE_TIME_DIGITIZED,
    DATE_TIME_ORIGINAL,
    EXIF_IFD_POINTER,
    parse_exif_datetime,
    read_capture_date,
)

EXIF_DATE_TIME = 0x0132
"""IFD0 `DateTime`: the software modification time. Never a capture date."""

SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)
SHOT_TEXT = "2018:07:14 15:32:05"
SHOT_TEXT_NUL = SHOT_TEXT + "\x00"


def write_image(
    path: Path,
    sub_ifd: dict[int, str] | None = None,
    ifd0: dict[int, str] | None = None,
    image_format: str = "JPEG",
) -> Path:
    """Write a small real image whose EXIF holds exactly the given tags."""
    exif = Image.Exif()
    for tag, value in (ifd0 or {}).items():
        exif[tag] = value
    if sub_ifd:
        exif.get_ifd(EXIF_IFD_POINTER).update(sub_ifd)

    kwargs = {"exif": exif.tobytes()} if (sub_ifd or ifd0) else {}
    Image.new("RGB", (16, 16), "gray").save(path, image_format, **kwargs)
    return path


class TestTheChain:
    def test_date_time_original_is_the_primary_source(self, tmp_path: Path) -> None:
        path = write_image(
            tmp_path / "shot.jpg",
            {
                DATE_TIME_ORIGINAL: "2018:07:14 15:32:05",
                DATE_TIME_DIGITIZED: "2019:01:01 00:00:00",
            },
        )

        assert read_capture_date(path) == CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)

    def test_date_time_digitized_is_the_fallback(self, tmp_path: Path) -> None:
        path = write_image(
            tmp_path / "scan.jpg", {DATE_TIME_DIGITIZED: "2018:07:14 15:32:05"}
        )

        assert read_capture_date(path) == CaptureDate(
            SHOT, CaptureSource.EXIF_DIGITIZED
        )

    def test_neither_tag_means_unknown(self, tmp_path: Path) -> None:
        path = write_image(tmp_path / "bare.jpg", {0x829A: "1/500"})

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_the_result_is_naive(self, tmp_path: Path) -> None:
        """RFC-028 section 5: the camera clock has no zone, so none is added."""
        path = write_image(
            tmp_path / "shot.jpg", {DATE_TIME_ORIGINAL: "2018:07:14 15:32:05"}
        )

        capture = read_capture_date(path)

        assert capture is not None
        assert capture.captured_at is not None
        assert capture.captured_at.tzinfo is None

    def test_an_unparseable_original_falls_through_to_digitized(
        self, tmp_path: Path
    ) -> None:
        """Garbage in one link degrades to the next, not to `unknown`."""
        path = write_image(
            tmp_path / "shot.jpg",
            {
                DATE_TIME_ORIGINAL: "not a date",
                DATE_TIME_DIGITIZED: "2018:07:14 15:32:05",
            },
        )

        assert read_capture_date(path) == CaptureDate(
            SHOT, CaptureSource.EXIF_DIGITIZED
        )


class TestWhatIsNotACaptureDate:
    """RFC-028 section 4: `mtime` is not in the chain, under any disguise."""

    def test_the_ifd0_date_time_tag_is_ignored(self, tmp_path: Path) -> None:
        """0x0132 is what editing software stamps on save: mtime in EXIF clothing."""
        path = write_image(tmp_path / "edited.jpg", ifd0={EXIF_DATE_TIME: SHOT_TEXT})

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_the_filesystem_modification_time_is_ignored(self, tmp_path: Path) -> None:
        """A file with an old mtime and no EXIF is still of unknown date."""
        path = write_image(tmp_path / "copied.jpg")
        timestamp = datetime.datetime(2018, 7, 14).timestamp()
        os.utime(path, (timestamp, timestamp))

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_original_placed_in_ifd0_is_not_found(self, tmp_path: Path) -> None:
        """Pins where the tag is read from, which is where cameras write it.

        A reader that looked in IFD0 would pass this suite's other fixtures
        only if they were built wrongly, and return `unknown` for a real
        camera roll. The inverse -- a tag misplaced in IFD0 -- is not a
        layout any camera produces, so not honouring it costs nothing.
        """
        path = write_image(tmp_path / "odd.jpg", ifd0={DATE_TIME_ORIGINAL: SHOT_TEXT})

        assert read_capture_date(path) == CaptureDate.unknown()


class TestPlaceholdersAndPadding:
    def test_the_all_zero_placeholder_is_absent_not_a_date(
        self, tmp_path: Path
    ) -> None:
        path = write_image(
            tmp_path / "unset-clock.jpg", {DATE_TIME_ORIGINAL: "0000:00:00 00:00:00"}
        )

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_the_zero_placeholder_falls_through_to_digitized(
        self, tmp_path: Path
    ) -> None:
        path = write_image(
            tmp_path / "unset-clock.jpg",
            {
                DATE_TIME_ORIGINAL: "0000:00:00 00:00:00",
                DATE_TIME_DIGITIZED: SHOT_TEXT,
            },
        )

        assert read_capture_date(path) == CaptureDate(
            SHOT, CaptureSource.EXIF_DIGITIZED
        )

    @pytest.mark.parametrize(
        "raw",
        ["0000:00:00 00:00:00", "    :  :     :  :  ", "", "   ", "\x00\x00"],
        ids=["zeros", "blank-with-colons", "empty", "spaces", "nuls"],
    )
    def test_placeholders_are_recognised_by_name(self, raw: str) -> None:
        assert parse_exif_datetime(raw) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "2018:07:14 15:32:05\x00",
            "2018:07:14 15:32:05\x00\x00\x00",
            "  2018:07:14 15:32:05  ",
            "2018:07:14 15:32:05\x00 ",
        ],
        ids=["nul", "nul-padding", "spaces", "nul-and-space"],
    )
    def test_nul_and_whitespace_padding_is_stripped(self, raw: str) -> None:
        assert parse_exif_datetime(raw) == SHOT

    def test_trailing_nul_survives_a_real_file(self, tmp_path: Path) -> None:
        """Pillow returns the terminator as part of the string; checked for real."""
        path = write_image(tmp_path / "padded.jpg", {DATE_TIME_ORIGINAL: SHOT_TEXT_NUL})

        assert read_capture_date(path) == CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)

    @pytest.mark.parametrize(
        "raw",
        [
            "2018-07-14 15:32:05",
            "2018:13:14 15:32:05",
            "2018:07:14 25:00:00",
            "2018:07:14",
            "2018:07:14 15:32:05+03:00",
            None,
            42,
            b"\xff\xfe",
        ],
        ids=[
            "iso-dashes",
            "month-13",
            "hour-25",
            "date-only",
            "zone-suffix",
            "none",
            "int",
            "non-ascii-bytes",
        ],
    )
    def test_anything_else_unparseable_is_absent(self, raw: object) -> None:
        assert parse_exif_datetime(raw) is None

    def test_ascii_bytes_are_accepted(self) -> None:
        assert parse_exif_datetime(b"2018:07:14 15:32:05\x00") == SHOT


class TestFilesWithoutExif:
    @pytest.mark.parametrize("image_format", ["PNG", "BMP", "JPEG"])
    def test_no_exif_is_unknown(self, tmp_path: Path, image_format: str) -> None:
        """The normal case for part of the supported extensions, not an anomaly."""
        path = write_image(
            tmp_path / f"plain.{image_format.lower()}", None, None, image_format
        )

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_exif_is_read_from_any_format_pillow_supports(self, tmp_path: Path) -> None:
        """A PNG *can* carry EXIF in an `eXIf` chunk, and then it counts."""
        path = write_image(
            tmp_path / "exported.png", {DATE_TIME_ORIGINAL: SHOT_TEXT}, None, "PNG"
        )

        assert read_capture_date(path) == CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)


class TestDamagedFilesNeverRaise:
    """RFC-028 section 13: a corrupt file degrades; the scan never sees it."""

    def test_a_file_that_is_not_an_image_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.jpg"
        path.write_bytes(b"this is a text file with a .jpg extension")

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_an_empty_file_is_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jpg"
        path.write_bytes(b"")

        assert read_capture_date(path) == CaptureDate.unknown()

    @pytest.mark.parametrize("keep", [4, 30, 120, 250])
    def test_a_truncated_file_is_unknown(self, tmp_path: Path, keep: int) -> None:
        whole = write_image(tmp_path / "whole.jpg", {DATE_TIME_ORIGINAL: SHOT_TEXT})
        path = tmp_path / "truncated.jpg"
        path.write_bytes(whole.read_bytes()[:keep])

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_a_corrupt_exif_segment_in_a_valid_jpeg_is_unknown(
        self, tmp_path: Path
    ) -> None:
        """The pixels decode; the APP1 payload is garbage behind a valid marker."""
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "gray").save(buffer, "JPEG")
        jpeg = buffer.getvalue()
        garbage = b"Exif\x00\x00" + b"MM\x00\x2a\xff\xff\xff\xff" + b"\x13" * 40
        app1 = b"\xff\xe1" + (len(garbage) + 2).to_bytes(2, "big") + garbage
        path = tmp_path / "corrupt-exif.jpg"
        path.write_bytes(jpeg[:2] + app1 + jpeg[2:])

        assert read_capture_date(path) == CaptureDate.unknown()

    def test_a_file_that_cannot_be_opened_is_not_examined(self, tmp_path: Path) -> None:
        """`None`, not `unknown`: an unreadable file says nothing about its bytes.

        Marking it `unknown` would stop every later scan from looking at a
        file that was merely locked, or on a disk that went away mid-scan
        -- the NULL / `unknown` collapse RFC-028 section 4.2 warns about.
        """
        assert read_capture_date(tmp_path / "vanished.jpg") is None

    def test_a_directory_in_place_of_a_file_is_not_examined(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "looks-like.jpg"
        folder.mkdir()

        assert read_capture_date(folder) is None

    def test_a_read_error_mid_parse_is_not_examined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An `OSError` with an errno is the disk failing, not the file.

        Pillow reports malformed bytes as `OSError("...")` with no errno,
        and those are `unknown`; the operating system reports a failed
        read -- a USB disk pulled mid-scan -- with one. Simulated, because
        a real mid-read device failure cannot be staged in a unit test.
        """
        path = write_image(tmp_path / "shot.jpg", {DATE_TIME_ORIGINAL: SHOT_TEXT})

        def failing_read(handle: object) -> None:
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(
            "app.infrastructure.filesystem.exif_capture_date._exif_sub_ifd",
            failing_read,
        )

        assert read_capture_date(path) is None
