"""The read-ahead thread of RFC-045 (section 9, "A thread").

Real files on `tmp_path`, scanned by the real `FilesystemImageProvider`
through `discovered_candidates()` -- the source the job runner builds -- so
that the order, the events and the ids are the ones a job sees. What a test
must be able to control is injected: the snapshot of change signals, the
limits, and, where a read has to block or fail, the read itself.
"""

from __future__ import annotations

import datetime
import gc
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from tests.application.fakes import make_device

from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.device import Device
from app.domain.services.indexing_observer import (
    IndexingObserver,
    NullIndexingObserver,
)
from app.domain.value_objects.change_signals import ChangeSignals
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
from app.infrastructure.workers import read_ahead
from app.infrastructure.workers.indexing_worker import discovered_candidates
from app.infrastructure.workers.read_ahead import ReadAheadCandidates, ReadAheadLimits

THREAD_NAME = "solidvision-read-ahead"
ROOMY = ReadAheadLimits(files=4, budget_bytes=10**9, max_file_bytes=10**9)


class RecordingObserver(IndexingObserver):
    """Writes nothing; remembers each call, and the thread it came from."""

    def __init__(self, log: list[tuple[str, object]] | None = None) -> None:
        self.log = log if log is not None else []
        self.threads: set[int] = set()

    def discovered(self, files_seen: int, complete: bool) -> None:
        self.threads.add(threading.get_ident())
        self.log.append(("discovered", (files_seen, complete)))

    def listed(self, listing: RootListing) -> None:
        self.threads.add(threading.get_ident())
        self.log.append(("listed", listing.file_count))

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self.threads.add(threading.get_ident())

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        self.threads.add(threading.get_ident())

    def should_stop(self) -> bool:
        self.threads.add(threading.get_ident())
        return False


@pytest.fixture()
def device() -> Device:
    return make_device(label="HD2")


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """Twelve photos in three folders, of different sizes, in mixed case."""
    for folder in ("2018", "a b", "B"):
        (tmp_path / folder).mkdir()
        for index in range(4):
            name = f"Photo_{index}.jpg" if index % 2 else f"photo_{index}.jpg"
            (tmp_path / folder / name).write_bytes(bytes([index]) * (100 + index))
    return tmp_path


def scan_of(
    disk: Path, device: Device
) -> Callable[[IndexingObserver], Iterator[IndexCandidate]]:
    """The scan the job runner builds, for the observer it is handed."""

    def scan(observer: IndexingObserver) -> Iterator[IndexCandidate]:
        provider = FilesystemImageProvider(
            disk,
            (".jpg",),
            extract_capture_date=False,
            extract_gps=False,
            observer=observer,
            report_every=3,
        )
        return discovered_candidates(provider, device, disk, [])

    return scan


def serial(disk: Path, device: Device) -> list[IndexCandidate]:
    return list(scan_of(disk, device)(NullIndexingObserver()))


def signals_of(
    candidates: list[IndexCandidate], changed: frozenset[str] = frozenset()
) -> dict[ImageId, ChangeSignals]:
    """The snapshot a previous run would leave, with `changed` files touched since."""
    return {
        candidate.image.id: ChangeSignals(
            file_size=candidate.file_size,
            file_modified_at=(
                candidate.file_modified_at - datetime.timedelta(seconds=10)
                if candidate.file_modified_at is not None
                and str(candidate.image.relative_path) in changed
                else candidate.file_modified_at
            ),
        )
        for candidate in candidates
    }


def reader_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == THREAD_NAME]


class TestOrder:
    def test_the_candidates_come_in_the_order_of_the_discovery(
        self, disk: Path, device: Device
    ) -> None:
        expected = [
            str(candidate.image.relative_path) for candidate in serial(disk, device)
        ]

        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY
        ) as candidates:
            seen = [str(candidate.image.relative_path) for candidate in candidates]

        assert seen == expected
        assert len(seen) == 12


class TestWhatIsRead:
    def test_every_file_is_read_when_nothing_is_indexed(
        self, disk: Path, device: Device
    ) -> None:
        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY
        ) as candidates:
            got = list(candidates)

        assert all(candidate.content is not None for candidate in got)
        assert candidates.stats.read == 12

    def test_only_the_files_the_snapshot_says_changed_are_read(
        self, disk: Path, device: Device
    ) -> None:
        touched = frozenset({"2018/Photo_1.jpg", "B/photo_2.jpg"})
        snapshot = signals_of(serial(disk, device), changed=touched)

        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), snapshot, ROOMY
        ) as candidates:
            got = list(candidates)

        read = {
            str(candidate.image.relative_path)
            for candidate in got
            if candidate.content is not None
        }
        assert read == touched
        assert candidates.stats.unchanged == 10

    def test_no_file_is_read_when_nothing_changed(
        self, disk: Path, device: Device
    ) -> None:
        """A re-scan of an indexed disk stays a `stat` per file (section 3, item 1)."""
        snapshot = signals_of(serial(disk, device))

        with ReadAheadCandidates(
            scan_of(disk, device),
            NullIndexingObserver(),
            snapshot,
            ROOMY,
            read_file=lambda path: pytest.fail(f"{path} was read"),
        ) as candidates:
            got = list(candidates)

        assert [candidate.content for candidate in got] == [None] * 12
        assert candidates.stats.read == 0

    def test_the_hash_is_the_one_the_hasher_computes(
        self, disk: Path, device: Device
    ) -> None:
        hasher = Sha256ContentHasher()

        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY
        ) as candidates:
            for candidate in candidates:
                assert candidate.content is not None
                assert candidate.content.content_hash == hasher.hash_image(
                    candidate.image
                )
                assert candidate.content.data == (
                    candidate.image.require_absolute_path().value.read_bytes()
                )


class TestTheLimits:
    def test_a_file_above_the_per_file_ceiling_is_left_to_be_read_by_path(
        self, disk: Path, device: Device
    ) -> None:
        # Sizes are 100..103 bytes: only the 100-byte files fit under 101.
        limits = ReadAheadLimits(files=4, budget_bytes=10**9, max_file_bytes=100)

        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, limits
        ) as candidates:
            got = list(candidates)

        assert {
            candidate.file_size for candidate in got if candidate.content is not None
        } == {100}
        assert candidates.stats.too_large == 9

    def test_a_full_budget_passes_files_on_without_waiting_for_it(
        self, disk: Path, device: Device
    ) -> None:
        """A window larger than the queue, holding every byte, must not hang.

        The consumer keeps every candidate -- as a window of 12 being
        decided would -- while the budget fits two files and the queue two
        items. A thread that waited for the budget to come back would wait
        for a consumer that is waiting for it.
        """
        limits = ReadAheadLimits(files=2, budget_bytes=205, max_file_bytes=10**9)
        held: list[IndexCandidate] = []

        def consume() -> None:
            with ReadAheadCandidates(
                scan_of(disk, device), NullIndexingObserver(), {}, limits
            ) as candidates:
                held.extend(candidates)

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        consumer.join(timeout=10)

        assert not consumer.is_alive(), "the read-ahead hung on a full budget"
        assert len(held) == 12
        assert [candidate.content is not None for candidate in held][:2] == [
            True,
            True,
        ]
        assert sum(candidate.content is not None for candidate in held) == 2

    def test_the_bytes_go_back_to_the_budget_when_released(
        self, disk: Path, device: Device
    ) -> None:
        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY
        ) as candidates:
            for candidate in candidates:
                assert candidate.content is not None
                candidate.content.release()

        assert candidates.budget.held == 0
        assert candidates.budget.peak > 0

    def test_a_content_nobody_released_goes_back_when_it_is_collected(
        self, disk: Path, device: Device
    ) -> None:
        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY
        ) as candidates:
            for candidate in candidates:
                pass
        del candidate
        gc.collect()

        assert candidates.budget.held == 0


class TestFailures:
    def test_an_unreadable_file_is_passed_on_to_fail_by_path(
        self, disk: Path, device: Device
    ) -> None:
        """One place for failures: the pipeline's, which reports the real error."""

        def read(path: Path) -> bytes:
            if path.name == "photo_2.jpg":
                raise PermissionError(f"cannot read {path}")
            return path.read_bytes()

        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY, read_file=read
        ) as candidates:
            got = list(candidates)

        assert len(got) == 12
        assert sum(candidate.content is None for candidate in got) == 3
        assert candidates.stats.failed == 3
        assert candidates.budget.held == sum(
            candidate.content.size for candidate in got if candidate.content
        )

    def test_the_exception_that_ended_the_scan_is_raised_where_it_happened(
        self, disk: Path, device: Device
    ) -> None:
        def scan(observer: IndexingObserver) -> Iterator[IndexCandidate]:
            candidates = scan_of(disk, device)(observer)
            for index, candidate in enumerate(candidates):
                if index == 3:
                    raise OSError("the disk went away")
                yield candidate

        seen: list[IndexCandidate] = []
        with pytest.raises(OSError, match="the disk went away"):
            with ReadAheadCandidates(
                scan, NullIndexingObserver(), {}, ROOMY
            ) as candidates:
                for candidate in candidates:
                    seen.append(candidate)

        assert len(seen) == 3
        assert reader_threads() == []


class TestTheEnd:
    def test_leaving_the_block_mid_queue_ends_the_thread(
        self, disk: Path, device: Device
    ) -> None:
        limits = ReadAheadLimits(files=2, budget_bytes=10**9, max_file_bytes=10**9)

        with ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, limits
        ) as candidates:
            first = next(iter(candidates))
            assert first.content is not None
            first.content.release()

        assert reader_threads() == []
        assert candidates.budget.held == 0, "the queued bytes were released"

    def test_the_end_waits_for_the_read_in_progress_and_no_more(
        self, disk: Path, device: Device
    ) -> None:
        reading = threading.Event()
        finish = threading.Event()

        def slow_read(path: Path) -> bytes:
            reading.set()
            finish.wait(timeout=10)
            return path.read_bytes()

        with ReadAheadCandidates(
            scan_of(disk, device),
            NullIndexingObserver(),
            {},
            ROOMY,
            read_file=slow_read,
        ):
            assert reading.wait(timeout=10)
            threading.Timer(0.2, finish.set).start()
            started = time.monotonic()

        assert time.monotonic() - started >= 0.15
        assert reader_threads() == []

    def test_an_interrupt_waits_for_the_thread_only_so_long(
        self, disk: Path, device: Device, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl+C ends the process; a read that cannot be cut short is left to it."""
        monkeypatch.setattr(read_ahead, "INTERRUPTED_JOIN_SECONDS", 0.2)
        reading = threading.Event()
        finish = threading.Event()

        def stuck_read(path: Path) -> bytes:
            reading.set()
            finish.wait(timeout=10)
            return path.read_bytes()

        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            with ReadAheadCandidates(
                scan_of(disk, device),
                NullIndexingObserver(),
                {},
                ROOMY,
                read_file=stuck_read,
            ):
                assert reading.wait(timeout=10)
                raise KeyboardInterrupt
        waited = time.monotonic() - started

        try:
            assert waited < 2.0
            assert [thread.daemon for thread in reader_threads()] == [True]
        finally:
            finish.set()
            for thread in reader_threads():
                thread.join(timeout=10)
        assert reader_threads() == []

    def test_iterating_outside_the_block_is_refused(
        self, disk: Path, device: Device
    ) -> None:
        candidates = ReadAheadCandidates(
            scan_of(disk, device), NullIndexingObserver(), {}, ROOMY
        )

        with pytest.raises(RuntimeError, match="inside its `with` block"):
            next(iter(candidates))


class TestTheScansEvents:
    """RFC-045 section 5.4: the events reach the observer on the consumer's thread."""

    def test_the_events_arrive_in_the_order_of_the_serial_scan(
        self, disk: Path, device: Device
    ) -> None:
        serial_log: list[tuple[str, object]] = []
        for candidate in scan_of(disk, device)(RecordingObserver(serial_log)):
            serial_log.append(("candidate", str(candidate.image.relative_path)))

        observer = RecordingObserver()
        with ReadAheadCandidates(
            scan_of(disk, device), observer, {}, ROOMY
        ) as candidates:
            for candidate in candidates:
                observer.log.append(("candidate", str(candidate.image.relative_path)))

        assert observer.log == serial_log
        assert observer.log[0] == ("listed", 12)
        assert observer.log[-1] == ("discovered", (12, True))

    def test_every_call_to_the_observer_happens_on_the_consuming_thread(
        self, disk: Path, device: Device
    ) -> None:
        """The defect section 8 names: a write from the wrong thread, by a race.

        The scan's observer is the job's, whose writes go through the job's
        session, which belongs to the thread that runs the pipeline.
        """
        observer = RecordingObserver()

        with ReadAheadCandidates(
            scan_of(disk, device), observer, {}, ROOMY
        ) as candidates:
            list(candidates)

        assert observer.threads == {threading.get_ident()}
        assert len(observer.log) >= 5
