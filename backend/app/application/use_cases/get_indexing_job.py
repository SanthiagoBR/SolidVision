"""Reading one job's progress, which is how a client follows a long run.

Polling rather than a push channel (RFC-029 section 7.2): the client is a
local UI asking for a number that moves every few seconds, and a
persistent connection would add connection management, reconnection and an
asynchronous path through the API to save requests that cost almost
nothing on localhost.
"""

from __future__ import annotations

from app.domain.entities.indexing_job import IndexingJob
from app.domain.exceptions import JobNotFoundError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.job_id import JobId


class GetIndexingJobUseCase:
    """Return one job, or say plainly that there is no such job."""

    def __init__(self, job_repository: IndexingJobRepository) -> None:
        self._jobs = job_repository

    def execute(self, job_id: JobId) -> IndexingJob:
        """Return the job, raising when the id names nothing.

        A missing job is an error rather than an empty answer, and the
        distinction is what lets a client tell "not finished yet" from
        "that id was never a job" while polling.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No indexing job with id {job_id}.")
        return job
