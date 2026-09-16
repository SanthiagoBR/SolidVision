"""Abstract repository port for indexing-job persistence (RFC-029).

Two properties of this port are unusual for the codebase, and both are
deliberate.

**Every write is conditional, and there is no plain `save()`.** Other
repositories here are stores: the caller owns the entity and writes it
back. A job has no single owner -- a route sets `cancel_requested` while a
worker writes counters, a reaper can take an abandoned job away from the
worker still holding it, and two workers may reach for the same queued job
in the same millisecond. So "write this only if the row is still in the
state I decided from" is the contract, not an implementation detail, and a
method that wrote unconditionally would be the one every future caller
reached for by mistake.

**It never decides anything.** The transitions live on `IndexingJob`; what
is here is the guarantee that a decided transition is applied atomically.
The one place that line blurs is `claim_next()`, which has to choose a row
and move it in a single statement -- and the contract test pins the result
against `IndexingJob.claim()` so the two cannot drift.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId


class IndexingJobRepository(ABC):
    """Repository contract for managing `IndexingJob` entities."""

    @abstractmethod
    def create(self, job: IndexingJob) -> IndexingJob:
        """Insert a new job, refusing a device that already has an active one.

        Raises `DeviceBusyError` when the device already has a `pending`
        or `running` job. **An implementation must not answer this with a
        query of its own**: the PostgreSQL implementation lets the partial
        unique index of RFC-029 section 9 refuse the insert and translates
        the failure, because a `SELECT` before the `INSERT` is a
        check-then-act and two concurrent requests would both pass it.

        The in-memory double has no index to lean on and therefore has to
        emulate the refusal. That is not optional politeness: a double
        that accepted two active jobs would make every Application test
        above it evidence about the double rather than about the system.

        Returns the stored job so that a caller sees server-assigned
        values -- `created_at` in particular -- rather than assuming its
        own.
        """

    @abstractmethod
    def get(self, job_id: JobId) -> IndexingJob | None:
        """Retrieve one job, or `None` when no row carries that id."""

    @abstractmethod
    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        """Write progress and a heartbeat onto a running job, and read it back.

        **One round trip, and that is the point.** RFC-029 section 9.1
        puts the heartbeat on the same write that already updates the
        counters, so proving the worker is alive costs nothing extra; and
        the row that comes back carries `cancel_requested`, so the worker
        learns whether it has been asked to stop from the same call.

        Writes **only** those columns. A whole-entity write here would
        carry the worker's own stale copy of `cancel_requested` -- read
        when it claimed the job, minutes ago, and `False` -- straight over
        a cancellation the route had recorded since. The bug is invisible:
        the job simply never stops.

        `checkpoint` of `None` leaves the stored checkpoint alone rather
        than clearing it: it means "nothing new is durable yet", which is
        what a full batch buffer looks like, and clearing would throw away
        a position a crash still needs.

        Returns `None` when the job is no longer running -- the reaper
        requeued it, or it was cancelled outright -- which tells the worker
        it is no longer the one holding this job.
        """

    @abstractmethod
    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        """Write `job` only while its row still holds `expected`.

        The compare-and-set the cancellation path is built from (RFC-029
        section 8). Cancelling a `pending` job is immediate, and the race
        it has to survive is a worker claiming that job in the moment
        between the read and the write; the `UPDATE ... WHERE status =
        'pending'` either wins or matches no row.

        Returns the stored job, or `None` when the row had moved on --
        which is not an error. The caller re-reads and decides again,
        which for cancellation means falling back to setting the flag on
        a job that is now running.
        """

    @abstractmethod
    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        """Atomically take the oldest queued job, or `None` when there is none.

        **Must not be implemented as select-then-update.** Two executors
        polling at once will pick the same row, and under `READ COMMITTED`
        the loser's `UPDATE` matches nothing and it goes back to sleep
        with work still queued -- a latency ghost that is invisible with a
        single executor, which is how every test runs. The PostgreSQL
        implementation selects `FOR UPDATE SKIP LOCKED` inside the
        `UPDATE`, and the contract test fires two concurrent claims and
        requires two different jobs.

        Oldest first, by `created_at`. There is no priority and no
        reordering (RFC-029 section 14).

        The claimed row is exactly `job.claim(now)` applied to the row as
        it stood -- `started_at` preserved on a resume, heartbeat stamped
        immediately -- and the contract test asserts that equality rather
        than trusting each implementation to have written the transition
        out the same way.
        """

    @abstractmethod
    def claim(self, job_id: JobId, now: datetime.datetime) -> IndexingJob | None:
        """Atomically take *this* job if it is still queued.

        What the CLI uses. `indexing_worker --root PATH` creates a job and
        then runs it in its own process, so it needs the same atomic move
        as the polling loop but aimed at one id -- and it has to lose
        gracefully when a separate executor got there first, in which case
        it follows the job by polling instead (RFC-029 section 12).

        Returns `None` when the job is absent or no longer `pending`.
        """

    @abstractmethod
    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        """Return running jobs whose last heartbeat predates `heartbeat_before`.

        The reaper's query, and only the query: what to *do* with an
        abandoned job -- back to the queue, cancelled, or finally failed
        -- is `IndexingJob.expire()`'s decision, and the reaper applies it
        through `save_if_status()` so that a job which came back to life
        between the two calls is left alone.

        A running job that has never emitted a heartbeat cannot exist:
        `claim()` stamps one. An implementation that returned rows with a
        `NULL` heartbeat would be reaping jobs it has no evidence about.
        """

    @abstractmethod
    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Return jobs, newest first, optionally narrowed by device or status.

        Newest first is part of the contract rather than a convenience:
        the history of a disk is read to answer "what happened last", and
        a list whose order depended on the implementation would make the
        in-memory double and PostgreSQL disagree about the first row a UI
        renders.
        """
