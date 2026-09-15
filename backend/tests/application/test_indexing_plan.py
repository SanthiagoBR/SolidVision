"""RFC-028 section 6.1: the capture date is not a change signal.

The test that matters here is written against `IndexMetadata`, not against
`IndexCandidate`, and the level is the whole point. `plan_indexing()` has no
parameter for a capture date, so a test that varied one on the candidate and
asserted "still skipped" would pass by construction -- and would keep
passing after someone added `existing.capture_source != ...` to the
comparison, because the prefetched metadata is where the stored source
actually arrives. These tests vary exactly that field and fail if the skip
decision ever starts reading it.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.application.use_cases.capture_date_plan import capture_date_to_write
from app.application.use_cases.indexing_plan import (
    IndexAction,
    IndexCandidate,
    plan_indexing,
)
from app.domain.entities.image import Image
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from tests.application.fakes import StubContentHasher
from tests.conftest import TEST_DEVICE_ID

MODIFIED_AT = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)
HASH = "a" * 64
SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)

EVERY_STORED_SOURCE = [None, *CaptureSource]


def candidate(
    capture: CaptureDate | None = None, file_size: int = 1024
) -> IndexCandidate:
    return IndexCandidate(
        image=Image(
            id=ImageId(uuid.UUID(int=1)),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath("fotos/2018/DJI_0042.JPG"),
            filename="DJI_0042",
            extension="jpg",
            captured_at=capture.captured_at if capture else None,
            capture_source=capture.source if capture else None,
        ),
        file_size=file_size,
        file_modified_at=MODIFIED_AT,
    )


class TestTheCaptureDateNeverDrivesTheSkipDecision:
    @pytest.mark.parametrize("stored", EVERY_STORED_SOURCE)
    def test_identical_signals_skip_whatever_the_stored_source(
        self, stored: CaptureSource | None
    ) -> None:
        """Same size, mtime and hash; any stored source -> `SKIP_UNCHANGED`."""
        existing = IndexMetadata(
            file_size=1024,
            file_modified_at=MODIFIED_AT,
            content_hash=HASH,
            capture_source=stored,
        )

        plan = plan_indexing(
            candidate(CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)),
            existing,
            StubContentHasher(default=HASH),
        )

        assert plan.action is IndexAction.SKIP_UNCHANGED

    def test_two_metadata_differing_only_in_capture_source_plan_identically(
        self,
    ) -> None:
        """The exact pair RFC-028 section 6.1 names, side by side."""
        never_examined = IndexMetadata(1024, MODIFIED_AT, HASH, capture_source=None)
        dated = IndexMetadata(
            1024, MODIFIED_AT, HASH, capture_source=CaptureSource.EXIF_ORIGINAL
        )
        hasher = StubContentHasher(default=HASH)

        first = plan_indexing(candidate(), never_examined, hasher)
        second = plan_indexing(candidate(), dated, hasher)

        assert first.action is second.action is IndexAction.SKIP_UNCHANGED
        assert hasher.hashed == []

    @pytest.mark.parametrize("stored", EVERY_STORED_SOURCE)
    def test_a_touched_identical_file_refreshes_whatever_the_stored_source(
        self, stored: CaptureSource | None
    ) -> None:
        """The hash branch must be just as blind to the source as the first."""
        existing = IndexMetadata(
            file_size=1024,
            file_modified_at=MODIFIED_AT - datetime.timedelta(days=1),
            content_hash=HASH,
            capture_source=stored,
        )

        plan = plan_indexing(candidate(), existing, StubContentHasher(default=HASH))

        assert plan.action is IndexAction.REFRESH_METADATA

    @pytest.mark.parametrize("stored", EVERY_STORED_SOURCE)
    def test_changed_bytes_embed_whatever_the_stored_source(
        self, stored: CaptureSource | None
    ) -> None:
        existing = IndexMetadata(
            file_size=2048,
            file_modified_at=MODIFIED_AT,
            content_hash=HASH,
            capture_source=stored,
        )

        plan = plan_indexing(candidate(), existing, StubContentHasher(default="b" * 64))

        assert plan.action is IndexAction.EMBED


class TestCaptureDateToWrite:
    """The conditional write of RFC-028 section 6, and the backfill's `--force`."""

    ORIGINAL = CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)
    DIGITIZED = CaptureDate(SHOT, CaptureSource.EXIF_DIGITIZED)
    UNKNOWN = CaptureDate.unknown()

    @pytest.mark.parametrize("discovered", [ORIGINAL, DIGITIZED, UNKNOWN])
    def test_a_row_never_examined_is_written(self, discovered: CaptureDate) -> None:
        """Including `unknown`: recording "examined, no date" is the point."""
        assert capture_date_to_write(None, discovered) == discovered

    @pytest.mark.parametrize("stored", list(CaptureSource))
    @pytest.mark.parametrize("discovered", [ORIGINAL, DIGITIZED, UNKNOWN])
    def test_an_examined_row_is_never_rewritten_by_a_scan(
        self, stored: CaptureSource, discovered: CaptureDate
    ) -> None:
        """What keeps re-scanning an unchanged collection free of writes."""
        assert capture_date_to_write(stored, discovered) is None

    @pytest.mark.parametrize("stored", [None, *CaptureSource])
    @pytest.mark.parametrize("force", [False, True])
    def test_an_unexamined_file_never_writes(
        self, stored: CaptureSource | None, force: bool
    ) -> None:
        """Extraction off, or unreadable: nobody read it, so nothing is recorded."""
        assert capture_date_to_write(stored, None, force=force) is None

    def test_force_upgrades_unknown_when_a_date_appears(self) -> None:
        """The case `--force` exists for: the extraction learned a new tag."""
        assert (
            capture_date_to_write(CaptureSource.UNKNOWN, self.ORIGINAL, force=True)
            == self.ORIGINAL
        )

    def test_force_upgrades_digitized_to_original(self) -> None:
        assert (
            capture_date_to_write(
                CaptureSource.EXIF_DIGITIZED, self.ORIGINAL, force=True
            )
            == self.ORIGINAL
        )

    def test_force_rewrites_an_equally_strong_source(self) -> None:
        """Equal or better may replace; a corrected parse of the same tag lands."""
        corrected = CaptureDate(SHOT.replace(second=6), CaptureSource.EXIF_ORIGINAL)

        assert (
            capture_date_to_write(CaptureSource.EXIF_ORIGINAL, corrected, force=True)
            == corrected
        )

    @pytest.mark.parametrize(
        ("stored", "discovered"),
        [
            (CaptureSource.EXIF_ORIGINAL, UNKNOWN),
            (CaptureSource.EXIF_ORIGINAL, DIGITIZED),
            (CaptureSource.EXIF_DIGITIZED, UNKNOWN),
        ],
        ids=["original-to-unknown", "original-to-digitized", "digitized-to-unknown"],
    )
    def test_force_never_downgrades(
        self, stored: CaptureSource, discovered: CaptureDate
    ) -> None:
        """The destructive naive implementation, ruled out by name.

        A file that fails to open today reads back as `unknown`; writing
        that over a stored `exif_original` would erase a real date and
        pass every test that only ever re-reads healthy files.
        """
        assert capture_date_to_write(stored, discovered, force=True) is None

    def test_force_skips_unknown_over_unknown_as_a_no_op(self) -> None:
        assert (
            capture_date_to_write(CaptureSource.UNKNOWN, self.UNKNOWN, force=True)
            is None
        )
