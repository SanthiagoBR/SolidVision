"""A job with the read-ahead on ends as it ends with it off (RFC-045 section 9).

The same tree of files, the same fakes, the same metadata window, run twice:
once by the pipeline of RFC-024, once with the files read ahead in a second
thread. The checkpoint, a cancellation, a file that fails, a disk that goes
away and a Ctrl+C must leave the job row, and the rows written, the same --
and no reading thread alive afterwards. The real SHA-256 is used in both, so
that the hash the read-ahead computes and the one the hasher computes are
compared too.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.application.fakes import RecordingContentHasher
from tests.infrastructure.workers.test_job_runner import World

from app.domain.entities.image import Image
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings
from app.infrastructure.workers.job_runner import JobRunner, build_runner
from app.infrastructure.workers.read_ahead import ReadAheadLimits

LIMITS = ReadAheadLimits(files=4, budget_bytes=10**6, max_file_bytes=10**6)
MODES = [pytest.param(None, id="serial"), pytest.param(LIMITS, id="read-ahead")]


class _FailsFor(EmbeddingModelPort):
    def __init__(self, filename: str) -> None:
        self._delegate = FakeEmbeddingModel()
        self._filename = filename

    def encode_image(
        self, image: Image, content: bytes | None = None
    ) -> EmbeddingVector:
        if image.filename == self._filename:
            raise ValueError(f"cannot decode {image.filename}")
        return self._delegate.encode_image(image)

    def encode_text(self, text: str) -> EmbeddingVector:
        return self._delegate.encode_text(text)


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """The tree of `test_job_runner.py`: sixteen photos in two folders."""
    for folder, count in (("2018", 10), ("2019", 6)):
        (tmp_path / folder).mkdir()
        for index in range(count):
            (tmp_path / folder / f"photo_{index:03d}.jpg").write_bytes(
                f"{folder}/{index}".encode()
            )
    return tmp_path


def reader_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "solidvision-read-ahead"
    ]


def world_of(
    disk: Path,
    read_ahead: ReadAheadLimits | None,
    model: EmbeddingModelPort | None = None,
) -> World:
    return World(
        disk,
        read_ahead=read_ahead,
        content_hasher=RecordingContentHasher(),
        embedding_model=model,
    )


def outcome(world: World, job: IndexingJob) -> tuple[object, ...]:
    """What a job leaves behind: its row, and the rows it wrote."""
    stored = world.reload(job)
    written = sorted(
        (str(record.image.relative_path), record.content_hash, record.file_size)
        for record in world.images.save_indexed_calls
    )
    return (
        stored.status,
        stored.progress,
        stored.last_processed_relative_path,
        stored.error_message,
        tuple(written),
    )


def run_both(
    disk: Path,
    prepare: Callable[[World, IndexingJob], None] | None = None,
    model_for: Callable[[], EmbeddingModelPort] | None = None,
) -> list[tuple[object, ...]]:
    """Run one job over `disk` serially, then one reading ahead; both outcomes."""
    results = []
    for limits in (None, LIMITS):
        world = world_of(disk, limits, model_for() if model_for else None)
        job = world.queue()
        if prepare is not None:
            prepare(world, job)
        world.runner.run_job(job.id)
        results.append(outcome(world, job))
        assert reader_threads() == []
    return results


class TestTheSameOutcome:
    def test_a_whole_disk(self, disk: Path) -> None:
        serial, ahead = run_both(disk)

        assert serial == ahead
        assert serial[0] is JobStatus.COMPLETED

    def test_a_cancellation(self, disk: Path) -> None:
        def cancel_after_the_first_batch(world: World, job: IndexingJob) -> None:
            saved: list[int] = []

            def cancel() -> None:
                saved.append(1)
                if len(saved) == 1:
                    world.jobs.save_if_status(
                        world.reload(job).request_cancel(), JobStatus.RUNNING
                    )

            world.images.on_save_indexed_many = cancel

        serial, ahead = run_both(disk, prepare=cancel_after_the_first_batch)

        assert serial == ahead
        assert serial[0] is JobStatus.CANCELLED

    def test_a_file_that_fails(self, disk: Path) -> None:
        serial, ahead = run_both(disk, model_for=lambda: _FailsFor("photo_003"))

        assert serial == ahead
        status, progress = serial[0], serial[1]
        assert status is JobStatus.COMPLETED
        assert isinstance(progress, IndexingProgress)
        assert progress.failed_images == 2

    def test_a_disk_pulled_mid_run(self, disk: Path) -> None:
        def pull_on_the_first_batch(world: World, job: IndexingJob) -> None:
            world.images.on_save_indexed_many = lambda: world.locator.disconnect(
                world.device.id
            )

        serial, ahead = run_both(disk, prepare=pull_on_the_first_batch)

        assert serial == ahead
        assert serial[0] is JobStatus.FAILED

    @pytest.mark.parametrize("limits", MODES)
    def test_a_ctrl_c_hands_the_job_back_with_its_checkpoint(
        self, disk: Path, limits: ReadAheadLimits | None
    ) -> None:
        world = world_of(disk, limits)
        saves: list[int] = []

        def interrupt_the_second_batch() -> None:
            saves.append(1)
            if len(saves) == 2:
                raise KeyboardInterrupt

        world.images.on_save_indexed_many = interrupt_the_second_batch
        job = world.queue()

        finished = world.runner.run_job(job.id)

        assert finished is not None
        assert finished.status is JobStatus.PENDING
        assert finished.attempts == 0
        assert str(finished.last_processed_relative_path).endswith("photo_003.jpg")
        for thread in reader_threads():
            thread.join(timeout=5)
        assert reader_threads() == []


class TestARescan:
    def test_a_new_disk_is_hashed_by_the_read_ahead_and_never_by_path(
        self, disk: Path
    ) -> None:
        hasher = RecordingContentHasher()
        world = World(disk, read_ahead=LIMITS, content_hasher=hasher)

        finished = world.runner.run_job(world.queue().id)

        assert finished is not None
        assert finished.progress.processed_images == 16
        assert hasher.hashed == []

    def test_an_unchanged_disk_is_skipped_whole(self, disk: Path) -> None:
        hasher = RecordingContentHasher()
        world = World(disk, read_ahead=LIMITS, content_hasher=hasher)
        world.runner.run_job(world.queue().id)

        finished = world.runner.run_job(world.queue().id)

        assert finished is not None
        assert finished.progress.processed_images == 0
        assert finished.progress.skipped_images == 16
        assert hasher.hashed == []
        assert world.images.list_change_signals_calls == [world.device.id] * 2

    def test_the_snapshot_is_read_on_the_thread_that_runs_the_job(
        self, disk: Path
    ) -> None:
        world = world_of(disk, LIMITS)
        threads: list[int] = []
        original = world.images.list_change_signals

        def recording(device_id: object) -> object:
            threads.append(threading.get_ident())
            return original(device_id)  # type: ignore[arg-type]

        world.images.list_change_signals = recording  # type: ignore[assignment]

        world.runner.run_job(world.queue().id)

        assert threads == [threading.get_ident()]


class TestTheComposition:
    def test_read_ahead_without_the_image_repository_is_refused(
        self, disk: Path
    ) -> None:
        world = world_of(disk, None)

        with pytest.raises(ValueError, match="image repository"):
            JobRunner(
                jobs=world.jobs,
                devices=world.devices,
                locator=world.locator,
                indexer=world.runner._indexer,
                supported_extensions=(".jpg",),
                read_ahead=LIMITS,
            )

    def test_the_production_runner_reads_ahead_with_a_window_of_the_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Section 5.7: the queue's size is the job's metadata window."""
        monkeypatch.setattr(settings, "indexing_read_ahead", True)
        monkeypatch.setattr(settings, "read_ahead_files", 32)

        runner = build_runner(job_session=object(), image_session=object())  # type: ignore[arg-type]

        assert runner._read_ahead == ReadAheadLimits(
            files=32,
            budget_bytes=settings.read_ahead_bytes,
            max_file_bytes=settings.read_ahead_max_file_bytes,
        )
        assert runner._images is runner._indexer._repository
        assert runner._indexer._metadata_prefetch_size == 32

    def test_switched_off_the_production_runner_is_the_pipeline_of_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "indexing_read_ahead", False)

        runner = build_runner(job_session=object(), image_session=object())  # type: ignore[arg-type]

        assert runner._read_ahead is None
        assert runner._indexer._metadata_prefetch_size == (
            settings.metadata_prefetch_size
        )

    def test_the_defaults_are_the_rfcs(self) -> None:
        defaults = {
            name: type(settings).model_fields[name].default
            for name in (
                "indexing_read_ahead",
                "read_ahead_files",
                "read_ahead_bytes",
                "read_ahead_max_file_bytes",
            )
        }

        assert defaults == {
            "indexing_read_ahead": True,
            "read_ahead_files": 32,
            "read_ahead_bytes": 512 * 1024 * 1024,
            "read_ahead_max_file_bytes": 64 * 1024 * 1024,
        }
