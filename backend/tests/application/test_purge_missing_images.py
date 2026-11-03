"""The purge of RFC-046 section 5.7, with every port in memory."""

from __future__ import annotations

import datetime

import pytest

from app.application.use_cases.purge_missing_images import PurgeMissingImagesUseCase
from app.domain.entities.image import Image
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError, DeviceNotFoundError
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_id import JobId
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from tests.application.fakes import (
    FakeDeviceRepository,
    FakeImageRepository,
    InMemoryThumbnailStore,
    make_device,
)

NOW = datetime.datetime(2026, 10, 31, 9, 0, tzinfo=datetime.UTC)
DEVICE = make_device(label="HD3")


class World:
    def __init__(self) -> None:
        self.images = FakeImageRepository()
        self.devices = FakeDeviceRepository([DEVICE])
        self.jobs = InMemoryIndexingJobRepository()
        self.thumbnails = InMemoryThumbnailStore()
        self.use_case = PurgeMissingImagesUseCase(
            image_repository=self.images,
            device_repository=self.devices,
            job_repository=self.jobs,
            thumbnail_store=self.thumbnails,
        )

    def indexed(self, path: str, thumbnail: bool = True) -> Image:
        relative = ImagePath(path)
        picture = Image(
            id=compute_image_id(DEVICE.id, relative),
            device_id=DEVICE.id,
            relative_path=relative,
            filename=relative.value.stem,
            extension="jpg",
        )
        location = self.thumbnails.save(picture.id, b"thumbnail") if thumbnail else None
        self.images.save_indexed(
            IndexingRecord(
                image=picture,
                embedding=EmbeddingVector([0.1, 0.2]),
                file_size=1,
                file_modified_at=NOW,
                content_hash="a" * 64,
                thumbnail_path=location,
            )
        )
        return picture

    def job(self, status: JobStatus) -> None:
        job = self.jobs.create(
            IndexingJob(id=JobId.new(), device_id=DEVICE.id, created_at=NOW)
        )
        if status is JobStatus.RUNNING:
            assert self.jobs.claim(job.id, NOW) is not None


def test_the_marked_rows_go_first_and_then_their_thumbnails() -> None:
    world = World()
    kept = world.indexed("a/kept.jpg")
    gone = world.indexed("a/gone.jpg")
    bare = world.indexed("a/bare.jpg", thumbnail=False)
    world.images.mark_presence([gone.id, bare.id], [], NOW)

    purged = world.use_case.execute(DEVICE.id)

    assert purged == 2
    assert world.images.get(gone.id) is None
    assert world.images.get(bare.id) is None
    assert world.images.get(kept.id) is not None
    assert world.thumbnails.deleted == [f"memory/{gone.id.value}.jpg"]
    assert set(world.thumbnails.saved) == {kept.id}


def test_nothing_marked_purges_nothing() -> None:
    world = World()
    world.indexed("a/kept.jpg")

    assert world.use_case.execute(DEVICE.id) == 0
    assert world.thumbnails.deleted == []


def test_an_unknown_device_is_not_found() -> None:
    world = World()

    with pytest.raises(DeviceNotFoundError):
        world.use_case.execute(make_device("\\\\?\\Volume{dead}\\").id)
    assert world.images.purge_missing_calls == []


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.RUNNING])
def test_a_disk_with_a_job_is_refused_and_nothing_is_deleted(
    status: JobStatus,
) -> None:
    """The job could unmark a row the purge would delete (section 5.7)."""
    world = World()
    gone = world.indexed("a/gone.jpg")
    world.images.mark_presence([gone.id], [], NOW)
    world.job(status)

    with pytest.raises(DeviceBusyError):
        world.use_case.execute(DEVICE.id)

    assert world.images.purge_missing_calls == []
    assert world.images.get(gone.id) is not None


def test_a_finished_job_does_not_hold_the_disk() -> None:
    world = World()
    gone = world.indexed("a/gone.jpg")
    world.images.mark_presence([gone.id], [], NOW)
    world.job(JobStatus.RUNNING)
    (running,) = world.jobs.list(device_id=DEVICE.id)
    world.jobs.save_if_status(running.complete(NOW), JobStatus.RUNNING)

    assert world.use_case.execute(DEVICE.id) == 1


def test_the_disk_does_not_have_to_be_connected() -> None:
    """No locator in the use case at all: the marks are enough."""
    world = World()
    gone = world.indexed("a/gone.jpg")
    world.images.mark_presence([gone.id], [], NOW)

    assert world.use_case.execute(DEVICE.id) == 1


class _LockedStore(InMemoryThumbnailStore):
    def delete(self, location: str) -> None:
        raise PermissionError(13, "being served", location)


def test_a_thumbnail_that_cannot_be_removed_does_not_undo_the_purge() -> None:
    world = World()
    world.thumbnails = _LockedStore()
    world.use_case = PurgeMissingImagesUseCase(
        image_repository=world.images,
        device_repository=world.devices,
        job_repository=world.jobs,
        thumbnail_store=world.thumbnails,
    )
    gone = world.indexed("a/gone.jpg")
    world.images.mark_presence([gone.id], [], NOW)

    assert world.use_case.execute(DEVICE.id) == 1
    assert world.images.get(ImageId(gone.id.value)) is None
