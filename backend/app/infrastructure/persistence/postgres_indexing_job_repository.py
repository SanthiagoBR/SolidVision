"""PostgreSQL-backed implementation of the indexing-job repository (RFC-029).

Three things here are load-bearing and easy to get subtly wrong.

**The claim is one statement.** `claim_next()` is an `UPDATE` whose `WHERE`
picks its row from a `SELECT ... FOR UPDATE SKIP LOCKED` subquery. Written
as a `SELECT` followed by an `UPDATE`, two executors polling at the same
moment choose the same id; under `READ COMMITTED` the loser's `UPDATE`
matches nothing and it goes back to sleep with work still queued. That is
not corruption, it is a latency ghost, and it is invisible with a single
executor -- which is how every test runs unless one is written for it.

**The unique-index violation is recognised by name.** `create()` catches
`IntegrityError` and looks for `uq_one_active_job_per_device` in the
constraint the driver names. Not the message text, which is localised and
reworded between versions, and not "any `IntegrityError`", which would
report a bad device foreign key as a busy disk.

**Every timestamp arrives from the caller's clock, and `now()` is never
used.** `now()` in PostgreSQL is the *start of the transaction*, which is
the trap RFC-029 section 4.11 names: with one commit per call it happens
to equal the wall clock today, and a later unit-of-work refactor would
break that agreement without a single test failing. The build prompt
proposed `clock_timestamp()` instead; this implementation takes the
caller's clock, for two reasons the prompt could not have weighed:

* the heartbeats that follow a claim are written through `save()` from an
  entity the worker has already built, so they are Python-clock values
  whatever this method does. Generating the *first* one in the database
  would put two clocks in one column, and the staleness test compares
  values within that column;
* the reaper has to be testable without sleeping (RFC-029's own
  validation table asks for a worker that "died"), and a clock the caller
  supplies is a clock a test can advance.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.infrastructure.database.models.indexing_job_model import (
    ACTIVE_JOB_INDEX,
    IndexingJobModel,
)


class PostgresIndexingJobRepository(IndexingJobRepository):
    """Repository implementation backed by PostgreSQL via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, job: IndexingJob) -> IndexingJob:
        """Insert the job, letting the partial unique index refuse a busy device.

        **There is no `SELECT` before this `INSERT`, and there must not
        be.** Checking for an active job in Python and then inserting is a
        check-then-act: two concurrent requests for the same disk both
        find nothing and both insert. The index of RFC-029 section 9 is
        where that race is actually decided, and this method's only job is
        to translate its refusal into a domain error, so that the route
        needs no `try/except` of its own (RFC-026 section 8).

        The rollback on failure follows `PostgresImageRepository`: a
        rejected insert must not leave the session in a pending-rollback
        state that fails the *next*, unrelated write.
        """
        model = IndexingJobModel.from_domain(job)
        try:
            self._session.add(model)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if _violates(exc, ACTIVE_JOB_INDEX):
                raise DeviceBusyError(
                    f"Device {job.device_id} already has an active indexing job."
                ) from exc
            raise
        except Exception:
            self._session.rollback()
            raise

        return model.to_domain()

    def get(self, job_id: JobId) -> IndexingJob | None:
        model = self._session.get(IndexingJobModel, job_id.value)
        return model.to_domain() if model is not None else None

    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        """Write the progress columns of a running job, and read the row back.

        Deliberately **not** `model.apply(job)`. That would write every
        column from the worker's own copy of the job -- including the
        `cancel_requested` it read when it claimed the row, which is
        `False` and minutes old -- straight over a cancellation the route
        recorded in the meantime. The job would then never stop, and
        nothing would look wrong anywhere.

        The row is re-read `FOR UPDATE` so the update and the read of
        `cancel_requested` are one step, which is what lets the worker
        learn it has been cancelled from the same round trip that proves
        it is alive (RFC-029 section 9.1).
        """
        try:
            model = self._session.get(
                IndexingJobModel, job_id.value, with_for_update=True
            )
            if model is None or model.status != JobStatus.RUNNING.value:
                self._session.rollback()
                return None

            model.discovered_files = progress.discovered_files
            model.processed_images = progress.processed_images
            model.skipped_images = progress.skipped_images
            model.failed_images = progress.failed_images
            model.discovery_complete = progress.discovery_complete
            model.last_heartbeat_at = heartbeat_at
            if checkpoint is not None:
                model.last_processed_relative_path = str(checkpoint)

            self._session.commit()
            return model.to_domain()
        except Exception:
            self._session.rollback()
            raise

    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        """Write the job only while its row still holds `expected`.

        The compare-and-set behind cancellation and behind the reaper.
        Both decide from a value they merely read, and both have to lose
        gracefully: a worker may claim a `pending` job in the moment
        between the read and the write, and an abandoned job may come back
        to life between the reaper's query and its verdict.

        The row is re-read `FOR UPDATE` rather than updated through a bare
        `UPDATE ... WHERE status = :expected`, because the entity carries
        every column and writing it through the model keeps one place that
        knows how a job is stored -- `IndexingJobModel.apply()`. The row
        lock is what makes the check and the write one step.
        """
        try:
            model = self._session.get(
                IndexingJobModel, job.id.value, with_for_update=True
            )
            if model is None or model.status != expected.value:
                self._session.rollback()
                return None
            model.apply(job)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return job

    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        """Atomically claim the oldest queued job, skipping rows already locked."""
        candidate = (
            select(IndexingJobModel.id)
            .where(IndexingJobModel.status == JobStatus.PENDING.value)
            .order_by(IndexingJobModel.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._claim(candidate, now)

    def claim(self, job_id: JobId, now: datetime.datetime) -> IndexingJob | None:
        """Atomically claim one named job, if it is still queued.

        The CLI's entry point (RFC-029 section 12): `indexing_worker
        --root PATH` creates a job and runs it in its own process, so it
        needs the same atomic move as the polling loop, aimed at one id.
        Returning `None` when a separate executor got there first is not a
        failure -- the CLI then follows that job by polling.
        """
        candidate = (
            select(IndexingJobModel.id)
            .where(
                IndexingJobModel.id == job_id.value,
                IndexingJobModel.status == JobStatus.PENDING.value,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._claim(candidate, now)

    def _claim(
        self, candidate: Select[tuple[uuid.UUID]], now: datetime.datetime
    ) -> IndexingJob | None:
        """Run the claiming `UPDATE` and return the row it moved, if any.

        **This SQL mirrors `IndexingJob.claim()`, and has to.** An atomic
        claim is one statement by definition, so the transition cannot be
        computed in Python from a row read beforehand. The duplication is
        deliberate, and it is held in check by the contract test, which
        asserts that what comes back equals `job.claim(now)` applied to
        the row as it stood -- so the two cannot drift apart in silence.

        `COALESCE` is `claim()`'s `self.started_at or now`: a resumed job
        keeps the moment it first started, because `created_at ->
        started_at` is the queue latency RFC-029 section 6.1 measures, and
        a job that crashed twice must not look like it started promptly.

        `synchronize_session=False` because nothing in this session holds
        a stale copy of the row worth reconciling -- the entity is rebuilt
        from the returned id below.
        """
        statement = (
            update(IndexingJobModel)
            .where(IndexingJobModel.id == candidate.scalar_subquery())
            .values(
                status=JobStatus.RUNNING.value,
                started_at=func.coalesce(IndexingJobModel.started_at, now),
                last_heartbeat_at=now,
            )
            .returning(IndexingJobModel.id)
            .execution_options(synchronize_session=False)
        )
        try:
            claimed_id = self._session.execute(statement).scalar_one_or_none()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        if claimed_id is None:
            return None
        model = self._session.get(IndexingJobModel, claimed_id)
        return model.to_domain() if model is not None else None

    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        """Return running jobs whose heartbeat has not moved recently enough.

        `last_heartbeat_at IS NOT NULL` is part of the query rather than
        implied by it. `claim()` always stamps a heartbeat, so a running
        row without one cannot exist -- and if one ever did, reaping it
        would mean acting on no evidence at all, which is the opposite of
        what a heartbeat is for.
        """
        statement = (
            select(IndexingJobModel)
            .where(
                IndexingJobModel.status == JobStatus.RUNNING.value,
                IndexingJobModel.last_heartbeat_at.is_not(None),
                IndexingJobModel.last_heartbeat_at < heartbeat_before,
            )
            .order_by(IndexingJobModel.created_at)
        )
        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]

    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Return matching jobs newest first, as the contract requires."""
        statement = select(IndexingJobModel).order_by(
            IndexingJobModel.created_at.desc()
        )
        if device_id is not None:
            statement = statement.where(IndexingJobModel.device_id == device_id.value)
        if status is not None:
            statement = statement.where(IndexingJobModel.status == status.value)

        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]


def _violates(error: IntegrityError, constraint: str) -> bool:
    """Whether `error` is the named constraint refusing, and not another one.

    Reads `psycopg`'s structured diagnostics rather than the message
    string. The message is localised and has been reworded between
    PostgreSQL versions, so matching on it would turn a busy-device 409
    into a 500 the day a server runs in another locale. The fallback to
    the rendered text exists only for drivers that expose no diagnostics,
    and is a last resort rather than the mechanism.
    """
    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    if name is not None:
        return bool(name == constraint)
    return constraint in str(error)
