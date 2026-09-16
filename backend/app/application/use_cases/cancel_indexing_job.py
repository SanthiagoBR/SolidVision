"""Stopping an indexing job, cooperatively (RFC-029 section 8).

Cancellation has two shapes and one entry point, and which shape applies
depends on whether anybody is holding the job:

* a **queued** job is cancelled outright. Nothing is running, so there is
  nothing to ask politely;
* a **running** job gets a flag. The worker owns every transition out of
  `running`, reads the flag between batches, finishes the batch in flight,
  writes its checkpoint and leaves. Setting `status` here instead would
  race the worker for that column, and would release the disk before the
  worker had actually let go of it.

And one refusal: cancelling a job that has already finished raises. The
RFC is explicit that this is an error rather than a silent no-op -- a
caller asking for it holds a wrong belief, and a 202 would confirm it.

**Nothing is undone.** A job cancelled at 60% leaves 60% of the scope
indexed and searchable, and the next job over the same scope skips it
through the incremental decision of RFC-020. Cancelling stops; it does not
reverse.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import IllegalJobTransitionError, JobNotFoundError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.job_id import JobId


class CancelIndexingJobUseCase:
    """Ask a job to stop, in whichever way its current state allows."""

    def __init__(
        self,
        job_repository: IndexingJobRepository,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._jobs = job_repository
        self._clock = clock or _utc_now

    def execute(self, job_id: JobId) -> IndexingJob:
        """Cancel `job_id`, or record that it should stop, and return the job.

        Written as read, decide, conditionally write -- twice at most, and
        never in a loop. The race worth naming is the first one: between
        reading a `pending` job and writing it `cancelled`, an executor
        can claim it. The conditional write then matches no row, and the
        second half of this method does the right thing for the job as it
        now is, rather than overwriting a running job with a cancelled one
        and leaving a worker indexing a job the database says is over.

        The bound is what makes this terminate. If the second conditional
        write also loses, the job finished on its own in the meantime --
        which is a genuine `IllegalJobTransitionError`, not something to
        retry.
        """
        job = self._require(job_id)
        # Asks the entity for permission before touching anything: a
        # finished job raises here, and the route needs no `if` of its own
        # (`AI_Context.md` -- Presentation decides no business rules).
        flagged = job.request_cancel()

        if job.status is JobStatus.PENDING:
            cancelled = self._jobs.save_if_status(
                job.cancel(self._clock()), JobStatus.PENDING
            )
            if cancelled is not None:
                return cancelled

            # A worker claimed it in between. Re-read and treat it as the
            # running job it now is -- including raising if it has already
            # finished, which is what `request_cancel()` does.
            job = self._require(job_id)
            flagged = job.request_cancel()

        if job.status is JobStatus.RUNNING:
            requested = self._jobs.save_if_status(flagged, JobStatus.RUNNING)
            if requested is not None:
                return requested

        raise IllegalJobTransitionError(
            f"Job {job_id} finished before the cancellation could be recorded."
        )

    def _require(self, job_id: JobId) -> IndexingJob:
        """Fetch the job, or say plainly that there is no such job.

        Distinct from "the job exists and has not started", which is an
        ordinary `pending` job somebody is polling. Collapsing the two
        would make a typo in an id indistinguishable from a slow queue.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No indexing job with id {job_id}.")
        return job


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
