"""The EXIF backfill's write policy (RFC-032 section 8.1), against the fake repository.

Migrated from RFC-028's `test_backfill_capture_dates.py` when the two backfills
became one: every capture-date case below is that file's, asserted now against
`summary.dates`, and the position cases sit beside them. The cases that only
make sense for one backfill writing two facts -- that the facts are judged
independently, that a file can lose one and keep the other -- are at the end.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Mapping

import pytest

from app.application.use_cases.backfill_exif import BackfillExifUseCase
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.image import Image
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.position import Position, PositionReading
from app.domain.value_objects.position_source import PositionSource
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)
ORIGINAL = CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL)
DIGITIZED = CaptureDate(SHOT, CaptureSource.EXIF_DIGITIZED)
UNKNOWN = CaptureDate.unknown()

FARM = PositionReading(Position(-26.321406, -48.816307), PositionSource.EXIF_GPS)
NOWHERE = PositionReading.unknown()


def image(
    name: str,
    capture: CaptureDate | None = None,
    position: PositionReading | None = None,
) -> Image:
    path = ImagePath(f"fotos/{name}.jpg")
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        device_id=TEST_DEVICE_ID,
        relative_path=path,
        filename=name,
        extension="jpg",
        captured_at=capture.captured_at if capture else None,
        capture_source=capture.source if capture else None,
        latitude=position.latitude if position else None,
        longitude=position.longitude if position else None,
        position_source=position.source if position else None,
    )


def seed(
    repository: FakeImageRepository,
    name: str,
    stored: CaptureDate | None = None,
    stored_position: PositionReading | None = None,
) -> None:
    """An indexed row, with whatever a previous run left on it."""
    repository.save_indexed(
        IndexingRecord(
            image=image(name, stored, stored_position),
            embedding=EmbeddingVector([0.1] * 8),
            file_size=1,
            file_modified_at=None,
        )
    )
    repository.save_indexed_calls.clear()


def candidate(
    name: str,
    read: CaptureDate | None,
    read_position: PositionReading | None = None,
) -> IndexCandidate:
    """What the scan found in the file today."""
    return IndexCandidate(
        image=image(name, read, read_position), file_size=1, file_modified_at=None
    )


def stored(repository: FakeImageRepository, name: str) -> CaptureDate | None:
    row = repository.get(image(name).id)
    assert row is not None
    return row.capture_date


def stored_position(
    repository: FakeImageRepository, name: str
) -> PositionReading | None:
    row = repository.get(image(name).id)
    assert row is not None
    return row.position_reading


def backfill(
    repository: FakeImageRepository,
    force: bool = False,
    dry_run: bool = False,
    metadata_prefetch_size: int = 512,
) -> BackfillExifUseCase:
    return BackfillExifUseCase(
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
        assert summary.dates.written == 2
        assert summary.dates.written_by_source == {
            "exif_original": 1,
            "unknown": 1,
        }

    def test_rows_never_examined_are_placed(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")
        seed(repository, "b")

        summary = backfill(repository).execute(
            [candidate("a", None, FARM), candidate("b", None, NOWHERE)]
        )

        assert stored_position(repository, "a") == FARM
        assert stored_position(repository, "b") == NOWHERE
        assert summary.positions.written == 2
        assert summary.positions.written_by_source == {"exif_gps": 1, "unknown": 1}

    def test_both_facts_come_from_one_candidate(self) -> None:
        """One read, two writes: the reason the two backfills became one."""
        repository = FakeImageRepository()
        seed(repository, "a")

        summary = backfill(repository).execute([candidate("a", ORIGINAL, FARM)])

        assert stored(repository, "a") == ORIGINAL
        assert stored_position(repository, "a") == FARM
        assert summary.dates.written == summary.positions.written == 1
        assert summary.discovered == 1

    def test_a_second_run_writes_nothing(self) -> None:
        """Idempotent: the default mode rereads nothing it already recorded."""
        repository = FakeImageRepository()
        seed(repository, "a")
        seed(repository, "b")
        found = [candidate("a", ORIGINAL, FARM), candidate("b", UNKNOWN, NOWHERE)]
        backfill(repository).execute(found)
        repository.update_capture_date_many_calls.clear()
        repository.update_position_many_calls.clear()

        second = backfill(repository).execute(found)

        assert second.dates.written == 0
        assert second.positions.written == 0
        assert second.dates.already_examined == 2
        assert second.positions.already_examined == 2
        assert repository.update_capture_date_many_calls == []
        assert repository.update_position_many_calls == []

    def test_an_unknown_row_is_not_reexamined(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a", stored=UNKNOWN, stored_position=NOWHERE)

        summary = backfill(repository).execute([candidate("a", ORIGINAL, FARM)])

        assert stored(repository, "a") == UNKNOWN
        assert stored_position(repository, "a") == NOWHERE
        assert summary.dates.written == 0
        assert summary.positions.written == 0

    def test_a_row_dated_before_rfc_032_still_gets_its_position(self) -> None:
        """The state every row indexed between RFC-028 and RFC-032 is in.

        Its date was examined and its position never was. Each fact's
        decision reads its own stored source, so the date is left alone and
        the position is written.
        """
        repository = FakeImageRepository()
        seed(repository, "a", stored=ORIGINAL)

        summary = backfill(repository).execute([candidate("a", ORIGINAL, FARM)])

        assert stored_position(repository, "a") == FARM
        assert summary.dates.written == 0
        assert summary.dates.already_examined == 1
        assert summary.positions.written == 1
        assert repository.update_capture_date_many_calls == []


class TestForceMode:
    def test_unknown_is_upgraded_when_the_file_now_yields_a_date(self) -> None:
        """The one case `--force` exists for: the extraction improved."""
        repository = FakeImageRepository()
        seed(repository, "a", stored=UNKNOWN)

        summary = backfill(repository, force=True).execute([candidate("a", ORIGINAL)])

        assert stored(repository, "a") == ORIGINAL
        assert summary.dates.written == 1

    def test_unknown_is_upgraded_when_the_file_now_yields_a_position(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a", stored_position=NOWHERE)

        summary = backfill(repository, force=True).execute([candidate("a", None, FARM)])

        assert stored_position(repository, "a") == FARM
        assert summary.positions.written == 1

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
        assert summary.dates.written == 0
        assert summary.dates.kept_stronger == 1
        assert repository.update_capture_date_many_calls == []

    def test_force_never_downgrades_exif_gps(self) -> None:
        """RFC-032 section 8.1: an `exif_gps` survives a file that reads `unknown`."""
        repository = FakeImageRepository()
        seed(repository, "a", stored_position=FARM)

        summary = backfill(repository, force=True).execute(
            [candidate("a", None, NOWHERE)]
        )

        assert stored_position(repository, "a") == FARM
        assert summary.positions.written == 0
        assert summary.positions.kept_stronger == 1
        assert repository.update_position_many_calls == []

    def test_an_equally_strong_reading_replaces_the_stored_one(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a", stored=ORIGINAL, stored_position=FARM)
        corrected = dataclasses.replace(ORIGINAL, captured_at=SHOT.replace(second=6))
        moved = PositionReading(Position(-26.3215, -48.8164), PositionSource.EXIF_GPS)

        backfill(repository, force=True).execute([candidate("a", corrected, moved)])

        assert stored(repository, "a") == corrected
        assert stored_position(repository, "a") == moved

    def test_never_examined_rows_are_filled_under_force_too(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")

        backfill(repository, force=True).execute([candidate("a", DIGITIZED, FARM)])

        assert stored(repository, "a") == DIGITIZED
        assert stored_position(repository, "a") == FARM


class TestTheTwoFactsAreIndependent:
    """RFC-032 section 8.1: "um arquivo pode perder a data e manter a posição"."""

    def test_a_file_can_lose_its_date_and_keep_its_position(self) -> None:
        """Under `--force`, the date is kept stronger and the position is rewritten.

        One decision per fact, each against its own stored source -- a
        single "is this row worth rewriting" judgement would either refuse
        the position because the date got weaker, or write both.
        """
        repository = FakeImageRepository()
        seed(repository, "a", stored=ORIGINAL, stored_position=FARM)
        moved = PositionReading(Position(-26.3215, -48.8164), PositionSource.EXIF_GPS)

        summary = backfill(repository, force=True).execute(
            [candidate("a", UNKNOWN, moved)]
        )

        assert stored(repository, "a") == ORIGINAL
        assert stored_position(repository, "a") == moved
        assert summary.dates.kept_stronger == 1
        assert summary.positions.written == 1

    def test_a_file_can_lose_its_position_and_keep_its_date(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a", stored=ORIGINAL, stored_position=FARM)
        corrected = dataclasses.replace(ORIGINAL, captured_at=SHOT.replace(second=6))

        summary = backfill(repository, force=True).execute(
            [candidate("a", corrected, NOWHERE)]
        )

        assert stored(repository, "a") == corrected
        assert stored_position(repository, "a") == FARM
        assert summary.dates.written == 1
        assert summary.positions.kept_stronger == 1

    def test_a_fact_not_examined_leaves_only_that_fact_alone(self) -> None:
        """A candidate with one fact `None` still has the other written."""
        repository = FakeImageRepository()
        seed(repository, "a")

        summary = backfill(repository).execute([candidate("a", None, FARM)])

        assert stored(repository, "a") is None
        assert stored_position(repository, "a") == FARM
        assert summary.not_readable == 0


class TestWhatTheBackfillNeverDoes:
    def test_a_dry_run_counts_but_writes_nothing(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")

        summary = backfill(repository, dry_run=True).execute(
            [candidate("a", ORIGINAL, FARM)]
        )

        assert summary.dates.written == 1
        assert summary.positions.written == 1
        assert stored(repository, "a") is None
        assert stored_position(repository, "a") is None
        assert repository.update_capture_date_many_calls == []
        assert repository.update_capture_date_calls == []
        assert repository.update_position_many_calls == []
        assert repository.update_position_calls == []
        assert "Would write" in summary.format_report()

    def test_a_file_with_no_row_is_not_created(self) -> None:
        """The backfill fills rows; only the indexing worker creates them."""
        repository = FakeImageRepository()

        summary = backfill(repository).execute([candidate("stray", ORIGINAL, FARM)])

        assert summary.not_indexed == 1
        assert repository.list() == []
        assert repository.save_indexed_calls == []

    def test_an_unreadable_file_leaves_its_row_unexamined(self) -> None:
        """`None` for both facts means nobody read it; a later run will retry."""
        repository = FakeImageRepository()
        seed(repository, "a")

        summary = backfill(repository).execute([candidate("a", None, None)])

        assert summary.not_readable == 1
        assert stored(repository, "a") is None
        assert stored_position(repository, "a") is None

    def test_no_embedding_is_written(self) -> None:
        repository = FakeImageRepository()
        seed(repository, "a")

        backfill(repository).execute([candidate("a", ORIGINAL, FARM)])

        assert repository.save_indexed_calls == []
        assert repository.save_indexed_many_calls == []


def test_writes_are_one_bulk_call_per_fact_per_prefetch_window() -> None:
    repository = FakeImageRepository()
    names = [f"photo_{index}" for index in range(5)]
    for name in names:
        seed(repository, name)

    backfill(repository, metadata_prefetch_size=2).execute(
        [candidate(name, ORIGINAL, FARM) for name in names]
    )

    assert [len(call) for call in repository.update_capture_date_many_calls] == [
        2,
        2,
        1,
    ]
    assert [len(call) for call in repository.update_position_many_calls] == [2, 2, 1]
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

        def update_position_many(
            self, readings: Mapping[ImageId, PositionReading]
        ) -> None:
            raise RuntimeError("deadlock detected")

        def update_position(self, image_id: ImageId, reading: PositionReading) -> None:
            if image_id == image("bad").id:
                raise RuntimeError("row rejected")
            super().update_position(image_id, reading)

    repository = _RejectsOne()
    seed(repository, "good")
    seed(repository, "bad")

    summary = backfill(repository).execute(
        [candidate("good", ORIGINAL, FARM), candidate("bad", ORIGINAL, FARM)]
    )

    assert summary.dates.written == 1
    assert summary.positions.written == 1
    assert [failure.path for failure in summary.failures] == [
        "fotos/bad.jpg",
        "fotos/bad.jpg",
    ]
    assert stored(repository, "good") == ORIGINAL
    assert stored_position(repository, "good") == FARM


def test_the_report_names_both_facts() -> None:
    repository = FakeImageRepository()
    seed(repository, "a")

    report = (
        backfill(repository).execute([candidate("a", ORIGINAL, FARM)]).format_report()
    )

    assert "EXIF backfill finished (only-unknown)" in report
    assert "exif_original=1" in report
    assert "exif_gps=1" in report
