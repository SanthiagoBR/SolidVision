"""The indexing job entity and the state machine that owns its transitions.

One job is one request to index a device, or some folders on it. It is an
*event*, not a description of the disk: two requests to index the same
folder are two rows, and the row keeps its counters and its checkpoint
long after the work is over (RFC-029 section 5.1).

**Every legal move lives here, and nowhere else.** The route does not
decide whether a cancellation is allowed, and the worker does not decide
whether an expired job may go back to the queue -- both call a method on
this entity and let it refuse (`AI_Context.md`: no business logic in
Presentation or Infrastructure). What Infrastructure owns is the
*atomicity* of a move, which is a different question and lives in the
repository.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, replace

from app.domain.exceptions import IllegalJobTransitionError
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope


class JobStatus(enum.StrEnum):
    """Where a job is in its life.

    A `StrEnum` so that the member *is* the string the database column
    holds. The column is `TEXT` rather than a PostgreSQL `ENUM` for the
    reason RFC-028 gave `capture_source`: adding a state later should not
    cost a migration.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether nothing further will ever happen to a job in this state."""
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """Whether this state holds the device.

        Exactly the states the partial unique index of RFC-029 section 9
        covers, named once so that the index, the in-memory double and
        the list filter cannot drift apart. `running` is obvious;
        `pending` is the one worth stating, and it is deliberate -- a
        queued job has already reserved the disk, so a second request for
        the same disk is refused rather than queued behind it.
        """
        return self in _ACTIVE


_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_ACTIVE = frozenset({JobStatus.PENDING, JobStatus.RUNNING})


class JobEvent(enum.StrEnum):
    """Everything that can be asked of a job, named so tests can enumerate it.

    Exists because "the legal transitions are tested" is a weaker claim
    than it sounds: asserting that the legal moves work says nothing about
    the illegal ones, and an entity that allowed `complete` on a cancelled
    job would pass such a suite. With the events as data,
    `test_indexing_job_transitions.py` walks the whole `(status, event)`
    grid and requires a refusal for every cell outside the table below.
    """

    CLAIM = "claim"
    COMPLETE = "complete"
    FAIL = "fail"
    CANCEL = "cancel"
    RELEASE = "release"
    EXPIRE = "expire"
    REQUEST_CANCEL = "request_cancel"


LEGAL_TRANSITIONS: dict[JobEvent, frozenset[JobStatus]] = {
    JobEvent.CLAIM: frozenset({JobStatus.PENDING}),
    JobEvent.COMPLETE: frozenset({JobStatus.RUNNING}),
    JobEvent.FAIL: frozenset({JobStatus.RUNNING}),
    JobEvent.CANCEL: frozenset({JobStatus.PENDING, JobStatus.RUNNING}),
    JobEvent.RELEASE: frozenset({JobStatus.RUNNING}),
    JobEvent.EXPIRE: frozenset({JobStatus.RUNNING}),
    JobEvent.REQUEST_CANCEL: frozenset({JobStatus.PENDING, JobStatus.RUNNING}),
}
"""Which states each event is allowed from. The whole state machine.

Read as a table rather than scattered through `if` statements, so that
the answer to "can a cancelled job be completed?" is one lookup and the
exhaustive test has something to enumerate.

Two entries are worth the words:

* `FAIL` is reachable only from `RUNNING`. A job that never started
  cannot have discovered that its disk is in a drawer -- the executor
  re-validates the connection *after* claiming, by which point the job is
  running.
* `EXPIRE` is likewise `RUNNING`-only, because a pending job has no
  heartbeat to miss.
"""


@dataclass(frozen=True)
class IndexingJob:
    """One indexing request, its progress, and where to resume it.

    Frozen, and every transition returns a new value. That follows `Image`
    and `Device` (RFC-009), and it earns its keep here in a way it does
    not there: a job is written by a route setting `cancel_requested` and
    by a worker writing counters at the same time, and an entity that
    mutated in place would make "which of those did I just overwrite?" an
    invisible question. A new value has to be handed to a repository,
    which is where that concurrency is actually resolved.
    """

    id: JobId
    device_id: DeviceId
    status: JobStatus = JobStatus.PENDING
    scopes: tuple[JobScope, ...] = ()
    """The folders to walk, already normalised, `()` for the whole device.

    Normalised by `normalize_scopes()` before it reaches here, so nothing
    downstream has to cope with one scope nested inside another (RFC-029
    section 5.2).
    """

    created_at: datetime.datetime | None = None
    started_at: datetime.datetime | None = None
    """When this job was *first* claimed, not when it was last claimed.

    Preserved across a resume on purpose. `created_at -> started_at` is
    the queue latency RFC-029 section 6.1 says to measure, and rewriting
    it every time a worker picks the job back up would make a job that
    crashed twice look like it had started promptly.
    """

    finished_at: datetime.datetime | None = None
    progress: IndexingProgress = IndexingProgress()
    last_processed_relative_path: ImagePath | None = None
    """The checkpoint: every file up to and including this one is durable.

    **Not "the last path seen".** The batch coordinator holds decided
    files in a pending buffer that survives across windows, so a later
    path can be decided while an earlier one is still unwritten; a
    checkpoint taken from the last path seen would step over that buffer,
    and a crash would lose exactly the images in it. See
    `IndexOrUpdateImagesUseCase.execute()`.

    Device-relative, like everything else persisted since RFC-027. An
    absolute checkpoint would stop being valid the moment the disk came
    back under another drive letter.
    """

    error_message: str | None = None
    last_heartbeat_at: datetime.datetime | None = None
    cancel_requested: bool = False
    """Set by the route; read by the worker between batches.

    A flag rather than a `cancelling` state, because the worker is the
    only owner of the transitions out of `running`. A state would put the
    route and the worker in a race for the `status` column, and would
    release the partial unique index before the worker had actually let
    go of the disk (RFC-029 section 8; the draft schema in section 5.1
    omitted this column entirely).
    """

    attempts: int = 0
    """How many times a worker was given this job and did not finish it.

    Incremented only by `expire()`. A clean stop does not count, and
    neither does the first claim: the number answers "how many times has
    something killed a worker holding this job", and it exists to stop the
    one case that would otherwise loop for ever -- a file whose decoder
    takes the whole process down, killing every worker that resumes onto
    it.
    """

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndexingJob):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def claim(self, now: datetime.datetime) -> IndexingJob:
        """Take the job out of the queue and start running it.

        `started_at` is written only the first time. The heartbeat is
        stamped immediately rather than at the first batch, because a
        worker that claimed a job and then spent two minutes walking a
        cold disk must not look abandoned before it has emitted anything
        (RFC-029 section 9.1).
        """
        self._require(JobEvent.CLAIM)
        return replace(
            self,
            status=JobStatus.RUNNING,
            started_at=self.started_at or now,
            last_heartbeat_at=now,
        )

    def complete(self, now: datetime.datetime) -> IndexingJob:
        """Finish successfully.

        The caller is required to have confirmed that the device is still
        mounted before asking for this (RFC-029 section 15). The entity
        cannot check it -- the Domain does not know what a mount point is
        -- which is why the rule is stated on the executor and tested
        there.
        """
        self._require(JobEvent.COMPLETE)
        return replace(self, status=JobStatus.COMPLETED, finished_at=now)

    def fail(self, now: datetime.datetime, message: str) -> IndexingJob:
        """Finish unsuccessfully, recording why.

        Does not touch `attempts`. A job that failed for a reason a retry
        would hit again -- an unplugged disk, a scope that is gone -- has
        not consumed a life, because nothing is going to retry it:
        RFC-029 section 14 keeps `failed` out of automatic resumption, and
        the user recreates it.
        """
        self._require(JobEvent.FAIL)
        return replace(
            self,
            status=JobStatus.FAILED,
            finished_at=now,
            error_message=message,
        )

    def cancel(self, now: datetime.datetime) -> IndexingJob:
        """Stop at the user's request, keeping everything already indexed.

        Legal from `pending`, where the route performs it directly because
        no worker holds the job, and from `running`, where only the worker
        performs it and only once the batch in flight has finished.
        Cancelling does not undo: a job cancelled at 60% leaves 60% of the
        scope searchable, and the next job over the same scope skips it
        through the incremental decision (RFC-029 section 8).
        """
        self._require(JobEvent.CANCEL)
        return replace(self, status=JobStatus.CANCELLED, finished_at=now)

    def release(self, now: datetime.datetime) -> IndexingJob:
        """Hand the job back to the queue after a *clean* stop.

        `Ctrl+C` on the executor arrives as `KeyboardInterrupt`, which is
        an operator stopping a process rather than a crash. The job goes
        back to `pending` with its checkpoint intact and **no attempt
        consumed**: counting it would mean that restarting the worker
        three times for ordinary reasons -- a reboot, a settings change --
        permanently failed a job that never went wrong.

        `now` is accepted and deliberately not written anywhere; the
        heartbeat is cleared instead, because a released job has nobody
        working on it and a fresh heartbeat would claim that somebody
        does. Keeping the parameter means every transition takes the
        clock, so no caller has to remember which ones need it.
        """
        self._require(JobEvent.RELEASE)
        del now
        return replace(self, status=JobStatus.PENDING, last_heartbeat_at=None)

    def expire(self, now: datetime.datetime, max_attempts: int) -> IndexingJob:
        """Resolve a job whose worker stopped proving that it was alive.

        Three outcomes, and the branching is here rather than in the
        reaper because it is a rule about what a job *is* rather than
        about how one is found:

        * a job somebody had already asked to cancel becomes `cancelled`.
          The worker is gone, so there is nothing left to stop, and the
          user's answer is still the right one -- reporting `failed` would
          blame the system for a stop the user chose;
        * a job with attempts left goes back to `pending`, keeping its
          checkpoint, so that a worker resumes rather than restarts. This
          is what gives the checkpoint column a consumer at all: marking
          the job `failed` and leaving the user to recreate it mints a new
          row with a `NULL` checkpoint, which re-reads the whole scope
          (RFC-029 section 9.1, corrected);
        * a job that has used up `max_attempts` becomes `failed`. Without
          that bound, a file that crashes the process outright would kill
          every worker that resumed onto it, for ever.

        The device stays reserved in the `pending` case, and that is the
        point: the partial unique index covers `pending` too, so an
        interrupted job at 60% keeps its place instead of losing the disk
        to whatever was requested next.
        """
        self._require(JobEvent.EXPIRE)
        if self.cancel_requested:
            return replace(self, status=JobStatus.CANCELLED, finished_at=now)

        attempts = self.attempts + 1
        if attempts >= max_attempts:
            return replace(
                self,
                status=JobStatus.FAILED,
                finished_at=now,
                attempts=attempts,
                error_message=(
                    f"Heartbeat expired {attempts} time(s); the worker holding "
                    "this job is presumed dead and no attempts remain."
                ),
            )
        return replace(
            self,
            status=JobStatus.PENDING,
            attempts=attempts,
            last_heartbeat_at=None,
        )

    def request_cancel(self) -> IndexingJob:
        """Record that the user wants this job stopped.

        Refuses on a job that has already finished. RFC-029 section 8 is
        explicit that this is an error rather than a silent no-op: a
        caller asking to cancel a `completed` job holds a wrong belief
        about the system, and a 202 would confirm it.

        Takes no clock, because it is not a transition -- the status does
        not move, and the worker is what eventually moves it.
        """
        self._require(JobEvent.REQUEST_CANCEL)
        return replace(self, cancel_requested=True)

    def with_progress(
        self,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob:
        """Record how far the run has got, and that it is still alive.

        Not a transition: the status does not move and no legality check
        applies. The heartbeat travels with the counters because the two
        are written together, which is what RFC-029 section 9.1 means by
        "no additional round trip" -- but *when* that write happens is a
        time policy owned by the Infrastructure observer, not by this
        method.

        The checkpoint is passed in rather than derived. Only the batch
        coordinator knows which files are durable, and `None` means it
        cannot name one yet -- everything decided so far is still in its
        pending buffer -- in which case the previous checkpoint stands
        rather than being cleared.
        """
        return replace(
            self,
            progress=progress,
            last_processed_relative_path=(
                checkpoint
                if checkpoint is not None
                else self.last_processed_relative_path
            ),
            last_heartbeat_at=heartbeat_at,
        )

    def _require(self, event: JobEvent) -> None:
        """Refuse `event` unless the current status permits it."""
        if self.status not in LEGAL_TRANSITIONS[event]:
            raise IllegalJobTransitionError(
                f"Cannot {event.value} a job that is {self.status.value}."
            )
