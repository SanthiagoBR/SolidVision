"""The backfill's write policy (RFC-028 section 7), against the fake repository."""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping

import pytest

from app.application.use_cases.backfill_capture_dates import (
    BackfillCaptureDatesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.image import Image
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)
ORIGINAL = CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)
DIGITIZED = CaptureDate(SHOT, CaptureSource.EXIF_DIGITIZED)
UNKNOWN = CaptureDate.unknown()


def image(name: str, capture: CaptureDate | None = None) -> Image:
    path = ImagePath(f"fotos/{name}.jpg")
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        device_id=TEST_DEVICE_ID,
        relative_path=path,
        filename=name,
        extension="jpg",
        captured_at=capture.captured_at if capture else None,
        capture_source=capture.source if capture else None,
    )


def seed(
    repository: FakeImageRepository, name: str, stored: CaptureDate | None = None
) -> None:
    """An indexed row, with whatever capture date a previous run left on it."""
    repository.save_indexed(
        IndexingRecord(
            image=image(name, stored),
            embedding=EmbeddingVector([0.1] * 8),
            file_size=1,
            file_modified_at=None,
        )
    )
    repository.save_indexed_calls.clear()


def candidate(name: str, read: CaptureDate | None) -> IndexCandidate:
    """What the scan found in the file today."""
    return IndexCandidate(image=image(name, read), file_size=1, file_modified_at=None)


def stored(repository: FakeImageRepository, name: str) -> CaptureDate | None:
    row = repository.get(image(name).id)
    assert row is not None
    return row.capture_date


def backfill(
    repository: FakeImageRepository,
    force: bool = False,
    dry_run: bool = False,
    metadata_prefetch_size: int = 512,
) -> BackfillCaptureDatesUseCase:
    return BackfillCaptureDatesUseCase(
        repository=repository,
        metadata_prefetch_size=metadata_prefetch_size,
        force=force,
        dry_run=dry_run,
    )


class TestOnlyUnknownMode:
    def test_rows_never_examined_are_dated(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")
        seed(repository, "b")

        summary = backfill(repository).execute(
            [candidate("a", ORIGINAL), candidate("b", UNKNOWN)]
        )

        assert stored(repository, "a") == ORIGINAL
        assert stored(repository, "b") == UNKNOWN
        assert summary.written == 2
        assert summary.written_by_source == {
            CaptureSource.EXIF_ORIGINAL: 1,
            CaptureSource.UNKNOWN: 1,
        }

    def test_a_second_run_writes_nothing(self) -> None:
        """Idempotent: the default mode rereads nothing it already recorded."""
        repository = FakeImageRepository()
        seed(repository, "a")
        seed(repository, "b")
        found = [candidate("a", ORIGINAL), candidate("b", UNKNOWN)]
        backfill(repository).execute(found)
        repository.update_capture_date_many_calls.clear()

        second = backfill(repository).execute(found)

        assert second.written == 0
        assert second.already_examined == 2
        assert repository.update_capture_date_many_calls == []

    def test_an_unknown_row_is_not_reexamined(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a", stored=UNKNOWN)

        summary = backfill(repository).execute([candidate("a", ORIGINAL)])

        assert stored(repository, "a") == UNKNOWN
        assert summary.written == 0


class TestForceMode:
    def test_unknown_is_upgraded_when_the_file_now_yields_a_date(self) -> None:
        """The one case `--force` exists for: the extraction improved."""
        repository = FakeImageRepository()
        seed(repository, "a", stored=UNKNOWN)

        summary = backfill(repository, force=True).execute([candidate("a", ORIGINAL)])

        assert stored(repository, "a") == ORIGINAL
        assert summary.written == 1

    @pytest.mark.parametrize("read", [UNKNOWN, DIGITIZED], ids=["unknown", "digitized"])
    def test_force_never_downgrades_exif_original(self, read: CaptureDate) -> None:
        """RFC-028 section 7 and the definition of done: no destructive re-read.

        The file reads weaker today -- damaged, or a reader regression --
        and the stored `exif_original` survives it, counted so the operator
        can look.
        """
        repository = FakeImageRepository()
        seed(repository, "a", stored=ORIGINAL)

        summary = backfill(repository, force=True).execute([candidate("a", read)])

        assert stored(repository, "a") == ORIGINAL
        assert summary.written == 0
        assert summary.kept_stronger == 1
        assert repository.update_capture_date_many_calls == []

    def test_an_equally_strong_reading_replaces_the_stored_one(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a", stored=ORIGINAL)
        corrected = dataclasses.replace(ORIGINAL, captured_at=SHOT.replace(second=6))

        backfill(repository, force=True).execute([candidate("a", corrected)])

        assert stored(repository, "a") == corrected

    def test_never_examined_rows_are_dated_under_force_too(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")

        backfill(repository, force=True).execute([candidate("a", DIGITIZED)])

        assert stored(repository, "a") == DIGITIZED


class TestWhatTheBackfillNeverDoes:
    def test_a_dry_run_counts_but_writes_nothing(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")

        summary = backfill(repository, dry_run=True).execute([candidate("a", ORIGINAL)])

        assert summary.written == 1
        assert stored(repository, "a") is None
        assert repository.update_capture_date_many_calls == []
        assert repository.update_capture_date_calls == []
        assert "Would write" in summary.format_report()

    def test_a_file_with_no_row_is_not_created(self) -> None:
        """The backfill dates rows; only the indexing worker creates them."""
        repository = FakeImageRepository()

        summary = backfill(repository).execute([candidate("stray", ORIGINAL)])

        assert summary.not_indexed == 1
        assert repository.list() == []
        assert repository.save_indexed_calls == []

    def test_an_unreadable_file_leaves_its_row_unexamined(self) -> None:
        """`None` from the scan means nobody read it; a later run will retry."""
        repository = FakeImageRepository()
        seed(repository, "a")

        summary = backfill(repository).execute([candidate("a", None)])

        assert summary.not_readable == 1
        assert stored(repository, "a") is None

    def test_no_embedding_is_written(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")

        backfill(repository).execute([candidate("a", ORIGINAL)])

        assert repository.save_indexed_calls == []
        assert repository.save_indexed_many_calls == []


def test_writes_are_one_bulk_call_per_prefetch_window() -> None:
    repository = FakeImageRepository()
    names = [f"photo_{index}" for index in range(5)]
    for name in names:
        seed(repository, name)

    backfill(repository, metadata_prefetch_size=2).execute(
        [candidate(name, ORIGINAL) for name in names]
    )

    assert [len(call) for call in repository.update_capture_date_many_calls] == [
        2,
        2,
        1,
    ]
    assert [len(ids) for ids in repository.get_index_metadata_many_calls] == [2, 2, 1]


def test_a_failed_bulk_write_degrades_to_per_row() -> None:
    class _RejectsOne(FakeImageRepository):
        def update_capture_date_many(
            self, captures: Mapping[ImageId, CaptureDate]
        ) -> None:
            raise RuntimeError("deadlock detected")

        def update_capture_date(self, image_id: ImageId, capture: CaptureDate) -> None:
            if image_id == image("bad").id:
                raise RuntimeError("row rejected")
            super().update_capture_date(image_id, capture)

    repository = _RejectsOne()
    seed(repository, "good")
    seed(repository, "bad")

    summary = backfill(repository).execute(
        [candidate("good", ORIGINAL), candidate("bad", ORIGINAL)]
    )

    assert summary.written == 1
    (failure,) = summary.failures
    assert failure.path.endswith("bad.jpg")
    assert stored(repository, "good") == ORIGINAL
