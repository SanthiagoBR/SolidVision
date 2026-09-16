"""In-memory indexing-job repository, held to the PostgreSQL contract (RFC-029).

**This double lies by omission unless it is built carefully**, and the two
places it would lie are the two properties the whole RFC rests on. If it
accepted a second active job for one device, every Application test above
it would be evidence about the double rather than about the system; if
its claim were not atomic, the one behaviour the polling loop depends on
would go untested everywhere except against a real database.

So both are emulated rather than skipped: the active-job rule is checked
on insert, and every mutation is taken under a lock so that two threads
calling `claim_next()` cannot both walk away with the same job.
"""

from __future__ import annotations

import dataclasses
import datetime
import threading
import uuid

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId


class InMemoryIndexingJobRepository(IndexingJobRepository):
    """Repository implementation backed by a dict and a lock."""

    def __init__(self) -> None:
        self._jobs: dict[uuid.UUID, IndexingJob] = {}
        self._lock = threading.Lock()
        """Stands in for the row locks PostgreSQL provides.

        `claim_next()` is a read and a write that must not interleave with
        another claim. In PostgreSQL that is `FOR UPDATE SKIP LOCKED`
        inside a single `UPDATE`; here it is this lock, held across the
        whole operation. Without it the double would hand the same job to
        two callers and the contract test would pass only against
        PostgreSQL -- which is exactly the class of difference a contract
        test exists to catch.
        """

    def create(self, job: IndexingJob) -> IndexingJob:
        """Insert, refusing a device that already has a pending or running job.

        The emulation of the partial unique index of RFC-029 section 9.
        It is a check-then-act, which the PostgreSQL implementation is
        forbidden to be -- and that is fine here precisely because the
        lock makes the check and the act one step, which is the property
        the index provides over there and a bare `SELECT` does not.
        """
        with self._lock:
            if any(
                existing.device_id == job.device_id and existing.status.is_active
                for existing in self._jobs.values()
            ):
                raise DeviceBusyError(
                    f"Device {job.device_id} already has an active indexing job."
                )
            stored = (
                job
                if job.created_at is not None
                else dataclasses.replace(
                    job, created_at=datetime.datetime.now(tz=datetime.UTC)
                )
            )
            self._jobs[stored.id.value] = stored
            return stored

    def get(self, job_id: JobId) -> IndexingJob | None:
        return self._jobs.get(job_id.value)

    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        """Write the progress columns of a running job and read it back.

        Applies the update to whatever the stored row is *now*, not to a
        copy the caller is holding, which is what makes the returned
        `cancel_requested` the route's value rather than the worker's
        stale one.
        """
        with self._lock:
            current = self._jobs.get(job_id.value)
            if current is None or current.status is not JobStatus.RUNNING:
                return None
            updated = current.with_progress(progress, checkpoint, heartbeat_at)
            self._jobs[job_id.value] = updated
            return updated

    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        """Write only while the stored row still holds `expected`."""
        with self._lock:
            current = self._jobs.get(job.id.value)
            if current is None or current.status is not expected:
                return None
            self._jobs[job.id.value] = job
            return job

    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        """Take the oldest queued job, under the lock, or return `None`."""
        with self._lock:
            pending = [
                job for job in self._jobs.values() if job.status is JobStatus.PENDING
            ]
            if not pending:
                return None
            oldest = min(pending, key=_created_at_key)
            return self._claim_locked(oldest, now)

    def claim(self, job_id: JobId, now: datetime.datetime) -> IndexingJob | None:
        with self._lock:
            job = self._jobs.get(job_id.value)
            if job is None or job.status is not JobStatus.PENDING:
                return None
            return self._claim_locked(job, now)

    def _claim_locked(self, job: IndexingJob, now: datetime.datetime) -> IndexingJob:
        """Apply the claim transition and store it; the lock is already held."""
        claimed = job.claim(now)
        self._jobs[claimed.id.value] = claimed
        return claimed

    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        """Return running jobs whose heartbeat has not moved recently enough.

        A running job with no heartbeat at all is not returned. `claim()`
        stamps one, so such a row cannot exist -- and treating it as stale
        would mean reaping a job on no evidence.
        """
        return [
            job
            for job in self._jobs.values()
            if job.status is JobStatus.RUNNING
            and job.last_heartbeat_at is not None
            and job.last_heartbeat_at < heartbeat_before
        ]

    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Return matching jobs newest first, as the contract requires."""
        jobs = [
            job
            for job in self._jobs.values()
            if (device_id is None or job.device_id == device_id)
            and (status is None or job.status is status)
        ]
        return sorted(jobs, key=_created_at_key, reverse=True)


def _created_at_key(job: IndexingJob) -> datetime.datetime:
    """Order by creation, putting a job with no timestamp first.

    `created_at` is stamped by `create()`, so `None` only reaches here for
    an entity somebody built by hand and saved directly. Sorting it to the
    front rather than raising keeps the double usable in tests that do not
    care about time, without inventing a timestamp that the row does not
    have.
    """
    return job.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC)
