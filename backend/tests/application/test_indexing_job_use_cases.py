"""The four job use cases (RFC-029 sections 7 and 8).

Everything runs against `InMemoryIndexingJobRepository`, which is held to
the same contract test as PostgreSQL -- including the refusal of a second
active job per device, which several tests here depend on being real.

The module is mostly about *refusals*, because that is where the decisions
are. Creating a job for a disk in a drawer, cancelling a job that finished
an hour ago, and naming a folder that is not on the disk are three
different failures with three different meanings, and flattening them into
one would be the whole of what RFC-029 section 7.1 asks not to do.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import pytest

from app.application.use_cases.cancel_indexing_job import CancelIndexingJobUseCase
from app.application.use_cases.create_indexing_job import CreateIndexingJobUseCase
from app.application.use_cases.get_indexing_job import GetIndexingJobUseCase
from app.application.use_cases.list_indexing_jobs import ListIndexingJobsUseCase
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import (
    DeviceBusyError,
    DeviceNotConnectedError,
    DeviceNotFoundError,
    IllegalJobTransitionError,
    InvalidJobScopeError,
    JobNotFoundError,
)
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    make_device,
)

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
BACKSLASH = chr(92)


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """A folder standing in for a mounted volume, with some photo folders."""
    for folder in ("2018", "2018/junho", "2019", "outros"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


class Fixture:
    """The four use cases wired to one in-memory world, over one movable clock.

    The clock is a field rather than a constant because "newest first" is
    a claim about `created_at`, and two jobs stamped with the same instant
    would let a list ordered by insertion pass a test about ordering by
    time.
    """

    def __init__(
        self,
        disk: Path | None,
        jobs: InMemoryIndexingJobRepository | None = None,
    ) -> None:
        self.now = NOW
        self.device = make_device(label="HD2")
        self.devices = FakeDeviceRepository([self.device])
        self.jobs = jobs or InMemoryIndexingJobRepository()
        self.locator = FakeDeviceLocator(
            {self.device.id: disk} if disk is not None else {}
        )
        self.create = CreateIndexingJobUseCase(
            self.jobs, self.devices, self.locator, clock=lambda: self.now
        )
        self.cancel = CancelIndexingJobUseCase(self.jobs, clock=lambda: self.now)
        self.get = GetIndexingJobUseCase(self.jobs)
        self.list = ListIndexingJobsUseCase(self.jobs)

    def advance(self, minutes: int = 1) -> None:
        self.now += datetime.timedelta(minutes=minutes)


class ClaimsBeforeTheFirstWrite(InMemoryIndexingJobRepository):
    """A repository that lets a worker in during the cancellation window.

    A subclass rather than a monkeypatch, so the interception is typed and
    the window it opens is described where it happens: exactly once,
    between the use case reading a `pending` job and writing the
    `cancelled` one it decided from.
    """

    def __init__(self) -> None:
        super().__init__()
        self.intercepted = False

    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        if not self.intercepted:
            self.intercepted = True
            self.claim(job.id, NOW)
        return super().save_if_status(job, expected)


@pytest.fixture()
def world(disk: Path) -> Fixture:
    return Fixture(disk)


class TestCreating:
    def test_a_job_for_a_connected_device_is_queued(self, world: Fixture) -> None:
        job = world.create.execute(world.device.id)

        assert job.status is JobStatus.PENDING
        assert job.device_id == world.device.id
        assert job.created_at == NOW

    def test_no_scopes_means_the_whole_device(self, world: Fixture) -> None:
        assert world.create.execute(world.device.id).scopes == ()

    def test_scopes_are_echoed_back_normalised(self, world: Fixture) -> None:
        """The client sees what will actually run (RFC-029 section 5.2).

        `2018/junho` is inside `2018`, so one folder is walked, not two --
        and a sequence that visited a file twice would have no positions
        in it for a checkpoint to name.
        """
        job = world.create.execute(world.device.id, ["2018", "2018/junho"])

        assert job.scopes == (JobScope("2018"),)

    def test_several_disjoint_scopes_all_survive(self, world: Fixture) -> None:
        job = world.create.execute(world.device.id, ["2019", "2018"])

        assert job.scopes == (JobScope("2018"), JobScope("2019"))

    def test_an_unknown_device_is_not_found(self, world: Fixture) -> None:
        with pytest.raises(DeviceNotFoundError):
            world.create.execute(DeviceId(uuid.uuid4()))

    def test_a_disconnected_device_is_refused(self) -> None:
        """409, not 400: the same request succeeds with the disk plugged in.

        RFC-029 section 7.1 sent this to 400; 400 says the request needs
        fixing, and nothing about it does.
        """
        world = Fixture(disk=None)

        with pytest.raises(DeviceNotConnectedError):
            world.create.execute(world.device.id)

    def test_the_connection_is_asked_about_every_time(self, world: Fixture) -> None:
        """Never cached: a disk can be pulled between two requests."""
        world.create.execute(world.device.id, ["2018"])
        world.jobs.save_if_status(
            world.jobs.list()[0].claim(NOW).complete(NOW), JobStatus.PENDING
        )
        world.create.execute(world.device.id, ["2019"])

        assert len(world.locator.mount_point_calls) == 2

    @pytest.mark.parametrize(
        "scope",
        [
            "..",
            "2018/../../etc",
            "D:/fotos",
            "/fotos",
            f"{BACKSLASH}fotos",
            "//server/share",
        ],
    )
    def test_a_scope_that_escapes_the_device_is_refused(
        self, world: Fixture, scope: str
    ) -> None:
        """Refused before anything touches the disk, so the folder need not exist."""
        with pytest.raises(InvalidJobScopeError):
            world.create.execute(world.device.id, [scope])

    def test_a_scope_that_is_not_on_the_disk_is_refused(self, world: Fixture) -> None:
        with pytest.raises(InvalidJobScopeError):
            world.create.execute(world.device.id, ["2020"])

    def test_a_scope_naming_a_file_rather_than_a_folder_is_refused(
        self, world: Fixture, disk: Path
    ) -> None:
        (disk / "2018" / "photo.jpg").write_bytes(b"data")

        with pytest.raises(InvalidJobScopeError):
            world.create.execute(world.device.id, ["2018/photo.jpg"])

    def test_a_second_job_for_a_busy_device_is_refused(self, world: Fixture) -> None:
        """Refused by the repository, which is refused by the index (section 9)."""
        world.create.execute(world.device.id)

        with pytest.raises(DeviceBusyError):
            world.create.execute(world.device.id)

    def test_nothing_is_queued_when_a_scope_is_refused(self, world: Fixture) -> None:
        """Validation happens before the insert, so a bad scope leaves no row.

        Otherwise a rejected request would leave the disk marked busy by a
        job nobody asked for.
        """
        with pytest.raises(InvalidJobScopeError):
            world.create.execute(world.device.id, ["2018", "2020"])

        assert world.list.execute() == []


class TestCancelling:
    def test_a_queued_job_is_cancelled_outright(self, world: Fixture) -> None:
        """Nothing is running, so there is nobody to ask politely."""
        job = world.create.execute(world.device.id)

        cancelled = world.cancel.execute(job.id)

        assert cancelled.status is JobStatus.CANCELLED
        assert cancelled.finished_at == NOW

    def test_a_running_job_is_flagged_rather_than_stopped(self, world: Fixture) -> None:
        """The worker owns every transition out of `running` (section 8).

        Writing `cancelled` here would race it for the `status` column and
        would release the disk before the worker had let go of it.
        """
        job = world.create.execute(world.device.id)
        world.jobs.claim(job.id, NOW)

        requested = world.cancel.execute(job.id)

        assert requested.status is JobStatus.RUNNING
        assert requested.cancel_requested is True

    @pytest.mark.parametrize(
        "finish",
        [
            lambda job: job.claim(NOW).complete(NOW),
            lambda job: job.claim(NOW).fail(NOW, "boom"),
            lambda job: job.cancel(NOW),
        ],
    )
    def test_cancelling_a_finished_job_is_an_error(
        self, world: Fixture, finish: object
    ) -> None:
        """A no-op 202 would confirm a belief the caller should not hold."""
        job = world.create.execute(world.device.id)
        world.jobs.save_if_status(finish(job), JobStatus.PENDING)  # type: ignore[operator]

        with pytest.raises(IllegalJobTransitionError):
            world.cancel.execute(job.id)

    def test_cancelling_an_unknown_job_is_not_found(self, world: Fixture) -> None:
        with pytest.raises(JobNotFoundError):
            world.cancel.execute(JobId(uuid.uuid4()))

    def test_a_worker_claiming_mid_cancellation_gets_the_flag_instead(
        self, disk: Path
    ) -> None:
        """The race RFC-029 section 8 has to survive, made deterministic.

        The job is `pending` when the use case reads it and `running` by
        the time it writes. The conditional write matches no row, and the
        second half of the use case sets the flag on the job as it now is
        -- rather than marking a job `cancelled` while a worker is still
        indexing it, which would release the disk and leave the worker
        writing to a job the database says is over.
        """
        jobs = ClaimsBeforeTheFirstWrite()
        world = Fixture(disk, jobs=jobs)
        job = world.create.execute(world.device.id)

        requested = world.cancel.execute(job.id)

        assert jobs.intercepted, "the race never happened; the test is vacuous"
        assert requested.status is JobStatus.RUNNING
        assert requested.cancel_requested is True

    def test_cancelling_leaves_the_indexed_work_alone(self, world: Fixture) -> None:
        """Cancelling stops; it does not reverse (RFC-029 section 8).

        The counters of the cancelled job still report what it managed to
        index, and the next job over the same scope skips those files
        through the incremental decision.
        """
        job = world.create.execute(world.device.id)
        world.jobs.claim(job.id, NOW)
        world.jobs.record_progress(
            job.id, IndexingProgress(processed_images=120), None, NOW
        )

        requested = world.cancel.execute(job.id)

        assert requested.progress.processed_images == 120


class TestReading:
    def test_a_job_is_readable_by_id(self, world: Fixture) -> None:
        job = world.create.execute(world.device.id)

        assert world.get.execute(job.id).id == job.id

    def test_an_unknown_job_is_not_found(self, world: Fixture) -> None:
        """ "Never existed" and "queued, not started" must not look alike."""
        with pytest.raises(JobNotFoundError):
            world.get.execute(JobId(uuid.uuid4()))

    def test_listing_returns_newest_first(self, world: Fixture) -> None:
        first = world.create.execute(world.device.id)
        world.jobs.save_if_status(first.cancel(NOW), JobStatus.PENDING)
        world.advance()
        second = world.create.execute(world.device.id)

        listed = world.list.execute()

        assert [job.id for job in listed][0] == second.id

    def test_listing_filters_by_status(self, world: Fixture) -> None:
        cancelled = world.create.execute(world.device.id)
        world.jobs.save_if_status(cancelled.cancel(NOW), JobStatus.PENDING)
        queued = world.create.execute(world.device.id)

        found = world.list.execute(status=JobStatus.PENDING)

        assert [job.id for job in found] == [queued.id]

    def test_listing_an_unknown_device_is_empty_rather_than_an_error(
        self, world: Fixture
    ) -> None:
        """A filter narrows a set; it does not assert that the set exists."""
        world.create.execute(world.device.id)

        assert world.list.execute(device_id=DeviceId(uuid.uuid4())) == []

    def test_listing_without_filters_returns_everything(self, world: Fixture) -> None:
        """Omitted must mean *all*, never *none*."""
        world.create.execute(world.device.id)

        assert len(world.list.execute()) == 1
