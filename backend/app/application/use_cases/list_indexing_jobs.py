"""Listing jobs, so a UI can show what happened to a disk (RFC-029 section 7.2)."""

from __future__ import annotations

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId


class ListIndexingJobsUseCase:
    """Return jobs, newest first, optionally narrowed by device or status."""

    def __init__(self, job_repository: IndexingJobRepository) -> None:
        self._jobs = job_repository

    def execute(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Return the matching jobs.

        An unknown device id is not rejected, for the reason RFC-027
        section 9 gives about search filters: this narrows a set rather
        than asserting that the named device exists, so a list for a disk
        the system has never seen correctly comes back empty. Rejecting it
        would mean a second lookup on every list request to answer a
        question nobody asked.

        Both filters omitted means every job, never no jobs -- the same
        trap the search route's `device_id` had to avoid.
        """
        return self._jobs.list(device_id=device_id, status=status)
