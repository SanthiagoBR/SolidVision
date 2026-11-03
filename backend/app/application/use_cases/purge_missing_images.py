"""Forgetting, by a button, the photos a disk no longer has (RFC-046 section 5.7).

The one way a row of `images` leaves the database since RFC-046, besides the
restart of RFC-045: the user asks for it, per disk, having been told how many.
What a scan does is mark (`SweepMissingImagesUseCase`); this deletes what is
marked.

    the request                         the answer
    a device nobody knows               DeviceNotFoundError -- 404
    a device with a pending or running  DeviceBusyError -- 409: the job may
    job                                 unmark a row this would delete
    otherwise                           the number of rows deleted

**The disk does not have to be plugged in.** The marks came from a scan that
was checked when it ran; forgetting what is no longer on a disk does not need
the disk.

**No guard of RFC-030.** Those protect the user's file system, and nothing
here touches it: the rows and the thumbnails are what this application made.
"""

from __future__ import annotations

from app.domain.entities.indexing_job import JobStatus
from app.domain.exceptions import DeviceBusyError, DeviceNotFoundError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.value_objects.device_id import DeviceId


class PurgeMissingImagesUseCase:
    """Delete a device's marked rows, then their thumbnails."""

    def __init__(
        self,
        image_repository: ImageRepository,
        device_repository: DeviceRepository,
        job_repository: IndexingJobRepository,
        thumbnail_store: ThumbnailStorePort,
    ) -> None:
        self._images = image_repository
        self._devices = device_repository
        self._jobs = job_repository
        self._thumbnails = thumbnail_store

    def execute(self, device_id: DeviceId) -> int:
        """Purge the marked rows of `device_id`, and return how many went.

        **The rows first, in one transaction, and only then the
        thumbnails.** A process that dies between the two leaves thumbnails
        without rows, which are litter in the application's cache; the
        other order would leave rows pointing at nothing.

        **Refused while the disk has a job** (`DeviceBusyError`): a running
        job ends with a sweep that unmarks the photos it found again, and a
        photo it found would lose its row here -- and be inferred again by
        the next scan. The job runner runs one job at a time, so with no
        active job nothing else writes the marks (section 5.4).
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        for status in (JobStatus.RUNNING, JobStatus.PENDING):
            if self._jobs.list(device_id=device_id, status=status):
                raise DeviceBusyError(
                    f"Device {device.label!r} has an indexing job; wait for it "
                    "to end before removing its missing photos."
                )

        purged = self._images.purge_missing(device_id)
        for location in purged.thumbnail_paths:
            try:
                self._thumbnails.delete(location)
            except OSError:
                # A thumbnail being served at this instant cannot be removed
                # on Windows. It stays as litter -- the failure this order
                # accepts -- and the rows, already gone, are the answer.
                continue
        return purged.count
