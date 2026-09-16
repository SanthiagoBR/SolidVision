"""`--root` as a job must index exactly what `--root` used to (RFC-029 §12).

The RFC's draft said the CLI becomes "a job with an empty scope, the whole
device". It does not, and this module is why: `--root D:/fotos/2018` has
never indexed a whole disk. An empty scope would have turned a refactor
into a behaviour change measured in hours, and it would have passed every
test written against a `tmp_path` that happens to *be* the mount point --
which is every test in the suite before this one.

So the comparison here is deliberately made over a **subfolder** of the
mount point, where the two readings differ, and it is made against the
`IndexingWorker` that existed before RFC-029 rather than against an
expectation typed out by hand.
"""

from __future__ import annotations

import datetime
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
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.infrastructure.workers.indexing_worker import (
    IndexingWorker,
    follow,
    scope_for,
)
from app.infrastructure.workers.job_runner import JobRunner

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
SUPPORTED = (".jpg", ".png")


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """A volume whose photos are in a subfolder, with decoys outside it."""
    photos = tmp_path / "fotos" / "2018"
    photos.mkdir(parents=True)
    for index in range(7):
        (photos / f"photo_{index:03d}.jpg").write_bytes(f"data-{index}".encode())

    elsewhere = tmp_path / "fotos" / "2019"
    elsewhere.mkdir()
    (elsewhere / "other.jpg").write_bytes(b"other")
    (tmp_path / "loose.jpg").write_bytes(b"loose")
    return tmp_path


def indexed(repository: FakeImageRepository) -> set[tuple[str, str]]:
    """What a run actually wrote, as (image id, device-relative path).

    The id is included rather than only the path, because the id is what a
    second code path could compute differently -- it is `uuid5` over
    `f"{device_id}/{relative_path}"`, and two orchestrators that disagreed
    about the mount point would produce two sets of rows for one file.
    """
    return {
        (str(record.image.id), str(record.image.relative_path))
        for record in repository.save_indexed_calls
    }


def pipeline(repository: FakeImageRepository) -> IndexOrUpdateImagesUseCase:
    return IndexOrUpdateImagesUseCase(
        repository=repository,
        embedding_model=FakeEmbeddingModel(),
        content_hasher=StubContentHasher(),
        batch_size=4,
        metadata_prefetch_size=8,
    )


class TestTheScopeDerivedFromRoot:
    def test_a_subfolder_becomes_a_relative_scope(self) -> None:
        scope = scope_for(Path("D:/fotos/2018"), Path("D:/"))

        assert str(scope) == "fotos/2018"
        assert scope.is_whole_device is False

    def test_the_mount_point_itself_becomes_the_whole_device(self) -> None:
        """The one case where "this folder" and "this disk" are one request."""
        assert scope_for(Path("D:/"), Path("D:/")).is_whole_device

    def test_a_root_outside_the_mount_point_is_refused(self) -> None:
        """`relative_to` raising is the right outcome, not something to paper over.

        A root that is not under the mount point the volume adapter
        reported means the two disagree, and guessing would file a photo
        under the wrong disk.
        """
        with pytest.raises(ValueError):
            scope_for(Path("E:/fotos"), Path("D:/"))


class TestEquivalenceWithTheWorkerItReplaces:
    def test_a_scoped_job_indexes_exactly_what_the_old_worker_did(
        self, disk: Path
    ) -> None:
        """The comparison RFC-029 section 17 asks for, over a subfolder.

        Both sides resolve the device the same way and measure paths from
        the same mount point; the only difference is that one is driven by
        a job row. If they disagree, the incremental decision has two
        homes -- which is the failure RFC-029 section 12 exists to
        prevent, and which shows up in the field as "the CLI re-indexes
        what the UI skips".
        """
        device = make_device(label="HD2")
        root = disk / "fotos" / "2018"

        before = FakeImageRepository()
        IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                root, SUPPORTED, extract_capture_date=False
            ),
            index_or_update_images_use_case=pipeline(before),
            device=device,
            mount_point=disk,
        ).run()

        after = FakeImageRepository()
        jobs = InMemoryIndexingJobRepository()
        runner = JobRunner(
            jobs=jobs,
            devices=FakeDeviceRepository([device]),
            locator=FakeDeviceLocator({device.id: disk}),
            indexer=pipeline(after),
            supported_extensions=SUPPORTED,
            extract_capture_date=False,
            clock=lambda: NOW,
        )
        job = jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=device.id,
                scopes=(scope_for(root, disk),),
                created_at=NOW,
            )
        )
        finished = runner.run_job(job.id)

        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert indexed(after) == indexed(before)
        assert len(indexed(after)) == 7

    def test_the_comparison_is_over_a_subfolder_and_not_the_whole_disk(
        self, disk: Path
    ) -> None:
        """Guards the test above from the shape that would make it vacuous.

        With `tmp_path` as both the mount point and the root, "this folder"
        and "the whole device" are the same set of files and the two
        readings of `--root` are indistinguishable. There have to be
        photos outside the scope for the comparison to mean anything.
        """
        before = FakeImageRepository()
        device = make_device(label="HD2")
        IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                disk, SUPPORTED, extract_capture_date=False
            ),
            index_or_update_images_use_case=pipeline(before),
            device=device,
            mount_point=disk,
        ).run()

        assert len(indexed(before)) > 7, "nothing lies outside the scope"


class TestFollowingAJobSomebodyElseRuns:
    def test_following_returns_once_the_job_is_finished(self) -> None:
        """The CLI's losing case: an executor claimed the job first.

        Not a failure and not a reason to exit -- the folder the user
        asked about is being indexed, and the command reports the same
        outcome either way.
        """
        jobs = InMemoryIndexingJobRepository()
        device = make_device()
        job = jobs.create(
            IndexingJob(id=JobId.new(), device_id=device.id, created_at=NOW)
        )
        claimed = jobs.claim(job.id, NOW)
        assert claimed is not None

        polls: list[float] = []

        def finish_after_two_polls(interval: float) -> None:
            polls.append(interval)
            if len(polls) == 2:
                jobs.save_if_status(claimed.complete(NOW), JobStatus.RUNNING)

        finished = follow(jobs, job.id, 0.5, sleep=finish_after_two_polls)

        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert polls == [0.5, 0.5]

    def test_following_a_job_that_is_already_finished_returns_at_once(self) -> None:
        jobs = InMemoryIndexingJobRepository()
        job = jobs.create(
            IndexingJob(id=JobId.new(), device_id=make_device().id, created_at=NOW)
        )
        jobs.save_if_status(job.cancel(NOW), JobStatus.PENDING)

        def never(interval: float) -> None:
            raise AssertionError("a finished job should not be polled")

        assert follow(jobs, job.id, 0.5, sleep=never) is not None

    def test_following_a_job_that_vanished_returns_nothing(self) -> None:
        jobs = InMemoryIndexingJobRepository()

        assert follow(jobs, JobId.new(), 0.5, sleep=lambda _: None) is None

    def test_a_scoped_job_that_is_not_scoped_at_all_still_runs(
        self, disk: Path
    ) -> None:
        """`--root` pointed at the mount point means the whole disk, and does."""
        device = make_device(label="HD2")
        jobs = InMemoryIndexingJobRepository()
        after = FakeImageRepository()
        runner = JobRunner(
            jobs=jobs,
            devices=FakeDeviceRepository([device]),
            locator=FakeDeviceLocator({device.id: disk}),
            indexer=pipeline(after),
            supported_extensions=SUPPORTED,
            extract_capture_date=False,
            clock=lambda: NOW,
        )
        job = jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=device.id,
                scopes=(),
                created_at=NOW,
            )
        )

        finished = runner.run_job(job.id)

        assert finished is not None
        assert finished.progress.processed_images == 9
        assert JobScope().is_whole_device
