"""`CaptureSource` and `CaptureDate`: the fallback chain of RFC-028 section 4."""

from __future__ import annotations

import datetime

import pytest

from app.domain.exceptions import InvalidCaptureDateError
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource

SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)


class TestCaptureSource:
    def test_the_persisted_values_are_fixed(self) -> None:
        """These strings are rows in the database; renaming one orphans data."""
        assert {member.value for member in CaptureSource} == {
            "exif_original",
            "exif_digitized",
            "unknown",
        }

    def test_there_is_no_member_for_the_filesystem_timestamp(self) -> None:
        """RFC-028 section 4: `mtime` is not in the chain, under any name."""
        for member in CaptureSource:
            assert "mtime" not in member.value
            assert "modified" not in member.value
            assert "file" not in member.value

    def test_it_is_a_string_so_it_persists_into_a_plain_column(self) -> None:
        assert isinstance(CaptureSource.EXIF_ORIGINAL, str)
        assert str(CaptureSource.EXIF_ORIGINAL) == "exif_original"
        assert CaptureSource("unknown") is CaptureSource.UNKNOWN

    def test_precedence_follows_the_fallback_chain(self) -> None:
        assert (
            CaptureSource.EXIF_ORIGINAL.precedence
            > CaptureSource.EXIF_DIGITIZED.precedence
            > CaptureSource.UNKNOWN.precedence
        )


class TestCaptureDate:
    def test_an_exif_date_pairs_with_its_source(self) -> None:
        capture = CaptureDate(captured_at=SHOT, source=CaptureSource.EXIF_ORIGINAL)

        assert capture.captured_at == SHOT
        assert capture.source is CaptureSource.EXIF_ORIGINAL

    def test_unknown_is_exactly_the_case_with_no_date(self) -> None:
        capture = CaptureDate.unknown()

        assert capture.captured_at is None
        assert capture.source is CaptureSource.UNKNOWN

    def test_a_zone_aware_date_is_rejected(self) -> None:
        """RFC-028 section 5: the camera recorded no zone, so none is invented."""
        with pytest.raises(InvalidCaptureDateError, match="time zone"):
            CaptureDate(
                captured_at=SHOT.replace(tzinfo=datetime.UTC),
                source=CaptureSource.EXIF_ORIGINAL,
            )

    def test_an_exif_source_without_a_date_is_rejected(self) -> None:
        with pytest.raises(InvalidCaptureDateError):
            CaptureDate(captured_at=None, source=CaptureSource.EXIF_ORIGINAL)

    def test_unknown_with_a_date_is_rejected(self) -> None:
        """A date labelled "unknown" is the disguised fallback §4 forbids."""
        with pytest.raises(InvalidCaptureDateError):
            CaptureDate(captured_at=SHOT, source=CaptureSource.UNKNOWN)
