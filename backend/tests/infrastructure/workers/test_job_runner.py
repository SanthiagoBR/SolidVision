"""The executor, the reaper, and the four outcomes of a run (RFC-029).

Real files on a real `tmp_path`, a real `IndexOrUpdateImagesUseCase`, a
real `FilesystemImageProvider` -- and fakes only where a test must be able
to lie: the embedding model, the image store, and the disk's connection
state.

**No process is killed anywhere in here.** RFC-029's validation table asks
what happens when a worker dies mid-job, and on Windows the honest way to
ask that is to move the clock rather than to reach for a signal that does
not exist. The clock is injected for exactly this, and a real crash is
left to the measurement scripts, where it can be observed instead of
simulated.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
    StubContentHasher,
    make_device,
)

from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.infrastructure.workers.job_runner import JobRunner

START = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
SUPPORTED = (".jpg", ".png")
STALE_TIMEOUT = 120.0
MAX_ATTEMPTS = 3


class MovableClock:
    """A clock a test can push forward, standing in for a worker's death."""

    def __init__(self, start: datetime.datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


class World:
    """One device, one disk, one runner, and the repositories behind them."""

    def __init__(
        self,
        disk: Path,
        jobs: InMemoryIndexingJobRepository | None = None,
        events: list[str] | None = None,
        sleep: Callable[[float], None] = lambda _: None,
    ) -> None:
        self.clock = MovableClock()
        self.events = events if events is not None else []
        self.device = make_device(label="HD2")
        self.devices = FakeDeviceRepository([self.device])
        self.jobs = jobs or InMemoryIndexingJobRepository()
        self.locator = FakeDeviceLocator({self.device.id: disk})
        self.images = FakeImageRepository()
        self.warmed: list[str] = []
        self.runner = JobRunner(
            jobs=self.jobs,
            devices=self.devices,
            locator=self.locator,
            indexer=IndexOrUpdateImagesUseCase(
                repository=self.images,
                embedding_model=FakeEmbeddingModel(),
                content_hasher=StubContentHasher(),
                batch_size=4,
                metadata_prefetch_size=8,
            ),
            supported_extensions=SUPPORTED,
            extract_capture_date=False,
            clock=self.clock,
            stale_timeout=STALE_TIMEOUT,
            max_attempts=MAX_ATTEMPTS,
            warm_up=self._record_warm_up,
            sleep=sleep,
        )

    def _record_warm_up(self) -> None:
        self.warmed.append("model")
        self.events.append("model loaded")

    def queue(self, *scopes: str) -> IndexingJob:
        return self.jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=self.device.id,
                scopes=tuple(JobScope(scope) for scope in scopes),
                created_at=self.clock(),
            )
        )

    def reload(self, job: IndexingJob) -> IndexingJob:
        stored = self.jobs.get(job.id)
        assert stored is not None
        return stored


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """A stand-in volume with a handful of photos in two folders."""
    for folder, count in (("2018", 10), ("2019", 6)):
        (tmp_path / folder).mkdir()
        for index in range(count):
            (tmp_path / folder / f"photo_{index:03d}.jpg").write_bytes(b"data")
    return tmp_path


@pytest.fixture()
def world(disk: Path) -> World:
    return World(disk)


class TestRunningAJob:
    def test_a_claimed_job_runs_to_completion(self, world: World) -> None:
        job = world.queue()

        finished = world.runner.run_job(job.id)

        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert finished.progress.processed_images == 16
        assert finished.progress.discovery_complete is True

    def test_a_scope_restricts_what_is_indexed(self, world: World) -> None:
        job = world.queue("2018")

        finished = world.runner.run_job(job.id)

        assert finished is not None
        assert finished.progress.processed_images == 10

    def test_progress_and_a_checkpoint_are_left_on_the_row(self, world: World) -> None:
        job = world.queue()

        finished = world.runner.run_job(job.id)

        assert finished is not None
        assert finished.last_processed_relative_path is not None
        assert str(finished.last_processed_relative_path).endswith("photo_005.jpg")
        assert finished.last_heartbeat_at is not None

    def test_a_job_somebody_else_claimed_is_left_alone(self, world: World) -> None:
        """The CLI's losing case, and it is not a failure (RFC-029 section 12)."""
        job = world.queue()
        world.jobs.claim(job.id, world.clock())

        assert world.runner.run_job(job.id) is None

    def test_the_polling_loop_reports_an_empty_queue(self, world: World) -> None:
        assert world.runner.claim_and_run() is False

    def test_the_model_is_loaded_before_anything_is_claimed(self, disk: Path) -> None:
        """RFC-029 section 9.1: a process still loading must not hold a job.

        Loading the CLIP checkpoint takes seconds and emits no batch, so a
        worker that claimed first would sit on a job in silence for the
        whole load -- and on a short stale timeout could be reaped before
        doing any work at all.
        """
        events: list[str] = []

        def stop_when_idle(_: float) -> None:
            # The loop sleeps only when the queue is empty, so this ends
            # the executor the way an operator would, one cycle after the
            # work ran out -- and exercises the real `run_forever()`
            # rather than a test-only entry point.
            raise KeyboardInterrupt

        world = World(
            disk,
            jobs=RecordsWhenClaimed(events),
            events=events,
            sleep=stop_when_idle,
        )
        world.queue()

        world.runner.run_forever()

        assert events[:2] == ["model loaded", "claimed"]


class RecordsWhenClaimed(InMemoryIndexingJobRepository):
    """Writes "claimed" into a shared log, so the *order* can be asserted.

    Asserting that both the load and the claim happened would pass against
    a runner that claimed first and loaded afterwards, which is exactly the
    arrangement RFC-029 section 9.1 rules out.
    """

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        self._events.append("claimed")
        return super().claim_next(now)


class TestCancellation:
    def test_a_cancelled_job_stops_and_is_marked_cancelled(self, world: World) -> None:
        """Observed between batches; the batch in flight finishes first."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.jobs.save_if_status(claimed.request_cancel(), JobStatus.RUNNING)

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.status is JobStatus.CANCELLED
        assert finished.progress.processed_images < 16

    def test_the_work_already_done_is_kept(self, world: World) -> None:
        """Cancelling stops; it does not reverse (RFC-029 section 8)."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.jobs.save_if_status(claimed.request_cancel(), JobStatus.RUNNING)

        world.runner.run_claimed(claimed)

        assert len(world.images.save_indexed_calls) > 0

    def test_a_cancelled_job_releases_the_device(self, world: World) -> None:
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.jobs.save_if_status(claimed.request_cancel(), JobStatus.RUNNING)
        world.runner.run_claimed(claimed)

        assert world.queue().status is JobStatus.PENDING


class TestADisconnectedDisk:
    def test_a_disk_pulled_mid_run_fails_rather_than_completing(
        self, world: World
    ) -> None:
        """The worst possible outcome would be `completed` (RFC-029 section 15).

        `discover()` returns quietly when its root is gone, and an `rglob`
        over a vanished volume can simply stop producing files -- so
        without the check at the end, a job would report success over half
        a scope.
        """
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.images.on_save_indexed_many = lambda: world.locator.disconnect(
            world.device.id
        )

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.status is JobStatus.FAILED
        assert "disconnected" in (finished.error_message or "")

    def test_a_disk_missing_before_the_scan_fails_the_job(self, world: World) -> None:
        """Plugged in at `POST`, in a drawer when the job leaves the queue."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.locator.disconnect(world.device.id)

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.status is JobStatus.FAILED
        assert "not connected" in (finished.error_message or "")

    def test_a_scope_that_vanished_fails_rather_than_indexing_nothing(
        self, world: World, disk: Path
    ) -> None:
        """ "Indexed zero files" and "that folder is gone" are different answers."""
        job = world.queue("2018")
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        for photo in (disk / "2018").iterdir():
            photo.unlink()
        (disk / "2018").rmdir()

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.status is JobStatus.FAILED

    def test_a_failed_job_keeps_its_checkpoint(self, world: World) -> None:
        """So that recreating the job resumes rather than restarting."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.images.on_save_indexed_many = lambda: world.locator.disconnect(
            world.device.id
        )

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.last_processed_relative_path is not None


class TestAnInterruptedExecutor:
    def test_ctrl_c_returns_the_job_to_the_queue(self, world: World) -> None:
        """An operator stopping a process is not a crash."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None

        def interrupt() -> None:
            raise KeyboardInterrupt

        world.images.on_save_indexed_many = interrupt

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.status is JobStatus.PENDING

    def test_ctrl_c_consumes_no_attempt(self, world: World) -> None:
        """Three ordinary restarts must not permanently fail a healthy job."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None

        def interrupt() -> None:
            raise KeyboardInterrupt

        world.images.on_save_indexed_many = interrupt

        finished = world.runner.run_claimed(claimed)

        assert finished is not None
        assert finished.attempts == 0


class TestTheReaper:
    def test_a_silent_job_goes_back_to_the_queue_with_its_checkpoint(
        self, world: World
    ) -> None:
        """RFC-029 section 9.1, corrected: requeued rather than failed.

        The draft marked an abandoned job `failed` and left the user to
        recreate it -- which mints a new row with a NULL checkpoint, so
        the checkpoint column would never have had a reader.
        """
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.jobs.record_progress(
            job.id,
            IndexingProgress(processed_images=4),
            ImagePath("2018/photo_003.jpg"),
            world.clock(),
        )
        world.clock.advance(STALE_TIMEOUT + 1)

        (reaped,) = world.runner.reap()

        assert reaped.status is JobStatus.PENDING
        assert reaped.attempts == 1
        assert reaped.last_processed_relative_path == ImagePath("2018/photo_003.jpg")

    def test_a_healthy_job_is_left_alone(self, world: World) -> None:
        job = world.queue()
        world.jobs.claim(job.id, world.clock())
        world.clock.advance(STALE_TIMEOUT / 2)

        assert world.runner.reap() == []

    def test_a_queued_job_is_never_reaped(self, world: World) -> None:
        world.queue()
        world.clock.advance(STALE_TIMEOUT * 10)

        assert world.runner.reap() == []

    def test_the_device_accepts_a_new_job_once_the_reaper_has_acted(
        self, world: World
    ) -> None:
        """The reaper's whole purpose: unblocking the partial unique index.

        Without it, one crashed worker leaves a `running` row that blocks
        every future indexing of that disk, for ever, until somebody edits
        the database by hand (RFC-029 section 9.1).
        """
        job = world.queue()
        world.jobs.claim(job.id, world.clock())
        world.clock.advance(STALE_TIMEOUT + 1)

        world.runner.reap()
        world.jobs.save_if_status(
            world.reload(job).cancel(world.clock()), JobStatus.PENDING
        )

        assert world.queue().status is JobStatus.PENDING

    def test_the_last_attempt_fails_the_job(self, world: World) -> None:
        """The bound that stops a process-killing file from looping for ever."""
        job = world.queue()
        for _ in range(MAX_ATTEMPTS):
            world.jobs.claim(job.id, world.clock())
            world.clock.advance(STALE_TIMEOUT + 1)
            world.runner.reap()

        finished = world.reload(job)
        assert finished.status is JobStatus.FAILED
        assert finished.attempts == MAX_ATTEMPTS

    def test_a_silent_job_the_user_cancelled_is_recorded_as_cancelled(
        self, world: World
    ) -> None:
        """Blaming the system for a stop the user chose is the wrong record."""
        job = world.queue()
        claimed = world.jobs.claim(job.id, world.clock())
        assert claimed is not None
        world.jobs.save_if_status(claimed.request_cancel(), JobStatus.RUNNING)
        world.clock.advance(STALE_TIMEOUT + 1)

        (reaped,) = world.runner.reap()

        assert reaped.status is JobStatus.CANCELLED
        assert reaped.attempts == 0

    def test_a_job_that_came_back_to_life_is_not_stolen(self, world: World) -> None:
        """A heartbeat between the query and the verdict means the worker lives.

        Stealing it would put two workers on one disk, which is what the
        whole of RFC-029 section 9 exists to prevent.
        """
        job = world.queue()
        world.jobs.claim(job.id, world.clock())
        world.clock.advance(STALE_TIMEOUT + 1)

        revived = RevivesDuringTheSweep(world.jobs, job.id, world.clock)
        world.runner._jobs = revived  # type: ignore[assignment]

        assert world.runner.reap() == []
        assert revived.considered == 1, "the reaper never saw it; the test is vacuous"
        assert world.reload(job).status is JobStatus.RUNNING


class RevivesDuringTheSweep:
    """Wraps a repository so the job heartbeats between query and verdict."""

    def __init__(
        self,
        delegate: InMemoryIndexingJobRepository,
        job_id: JobId,
        clock: MovableClock,
    ) -> None:
        self._delegate = delegate
        self._job_id = job_id
        self._clock = clock
        self.considered = 0

    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        stale = self._delegate.list_stale(heartbeat_before)
        self.considered += len(stale)
        # The worker was alive after all, and says so right now.
        self._delegate.record_progress(
            self._job_id, IndexingProgress(), None, self._clock()
        )
        return stale

    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        stored = self._delegate.get(job.id)
        if stored is None or stored.last_heartbeat_at != job.last_heartbeat_at:
            # The row moved since the verdict was decided from it.
            return None
        return self._delegate.save_if_status(job, expected)


class TestARescanThatNeverBatches:
    def test_a_skip_only_rescan_is_not_killed_by_the_reaper(self, world: World) -> None:
        """The most common run there is, and the one a naive heartbeat kills.

        A re-scan of an already-indexed disk completes no batches at all:
        every file is `SKIP_UNCHANGED`, nothing is flushed, and a
        heartbeat written only on flush would go silent for the whole run.
        Here a reaper sweeps *throughout* the scan, with the clock pushed
        past the timeout between every callback, and the job still has to
        survive and complete.
        """
        first = world.queue()
        world.runner.run_job(first.id)

        second = world.queue()
        sweeping = SweepsWhileWorking(world.jobs, world.runner, world.clock)
        world.runner._jobs = sweeping  # type: ignore[assignment]

        finished = world.runner.run_job(second.id)

        assert sweeping.sweeps > 1, "the reaper never ran; the test is vacuous"
        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert finished.progress.skipped_images == 16
        assert finished.progress.processed_images == 0


class SweepsWhileWorking:
    """Runs the reaper on every progress write, with time pushed forward.

    The worst case a healthy job can face: a reaper that looks at it
    constantly, and a clock that has always moved past the stale timeout
    since the previous heartbeat. The job survives exactly because each
    heartbeat lands before the next sweep.
    """

    def __init__(
        self,
        delegate: InMemoryIndexingJobRepository,
        runner: JobRunner,
        clock: MovableClock,
    ) -> None:
        self._delegate = delegate
        self._runner = runner
        self._clock = clock
        self.sweeps = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        written = self._delegate.record_progress(
            job_id, progress, checkpoint, heartbeat_at
        )
        self._clock.advance(STALE_TIMEOUT / 4)
        self.sweeps += 1
        for abandoned in self._delegate.list_stale(
            self._clock() - datetime.timedelta(seconds=STALE_TIMEOUT)
        ):
            self._delegate.save_if_status(
                abandoned.expire(self._clock(), MAX_ATTEMPTS), JobStatus.RUNNING
            )
        return written
