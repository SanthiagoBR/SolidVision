"""The indexing-job state machine, checked over the whole grid (RFC-029).

The point of this module is the *illegal* half. A suite that exercises
only the legal moves passes just as happily against an entity with no
checks at all, so every `(status, event)` pair is enumerated below and
each one has to either be in `LEGAL_TRANSITIONS` and work, or refuse.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable

import pytest
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.indexing_job import (
    LEGAL_TRANSITIONS,
    IndexingJob,
    JobEvent,
    JobStatus,
)
from app.domain.exceptions import IllegalJobTransitionError
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(minutes=5)
MAX_ATTEMPTS = 3


def job(status: JobStatus = JobStatus.PENDING, **overrides: object) -> IndexingJob:
    """Build a job in a given state, with everything else left at default."""
    fields: dict[str, object] = {
        "id": JobId(uuid.uuid4()),
        "device_id": TEST_DEVICE_ID,
        "status": status,
        "created_at": NOW,
    }
    fields.update(overrides)
    return IndexingJob(**fields)  # type: ignore[arg-type]


INVOCATIONS: dict[JobEvent, Callable[[IndexingJob], IndexingJob]] = {
    JobEvent.CLAIM: lambda j: j.claim(LATER),
    JobEvent.COMPLETE: lambda j: j.complete(LATER),
    JobEvent.FAIL: lambda j: j.fail(LATER, "boom"),
    JobEvent.CANCEL: lambda j: j.cancel(LATER),
    JobEvent.RELEASE: lambda j: j.release(LATER),
    JobEvent.EXPIRE: lambda j: j.expire(LATER, MAX_ATTEMPTS),
    JobEvent.REQUEST_CANCEL: lambda j: j.request_cancel(),
}
"""One call per event, so the grid below can be driven by data.

Kept beside the enum rather than inside the entity: how an event is
spelled as a method call is a fact about the test, and putting it in the
entity would let a method be renamed without anything noticing.
"""


def test_every_event_has_an_invocation() -> None:
    """The grid is only exhaustive if this table covers the enum."""
    assert set(INVOCATIONS) == set(JobEvent)
    assert set(LEGAL_TRANSITIONS) == set(JobEvent)


@pytest.mark.parametrize("status", list(JobStatus))
@pytest.mark.parametrize("event", list(JobEvent))
def test_every_illegal_pair_is_refused(status: JobStatus, event: JobEvent) -> None:
    """Every cell outside `LEGAL_TRANSITIONS` raises, and every cell in it does not.

    Both directions in one test on purpose. Asserting only the refusals
    would pass against an entity that refuses everything, and asserting
    only the successes is the weak suite this module exists to avoid.
    """
    subject = job(status)

    if status in LEGAL_TRANSITIONS[event]:
        assert isinstance(INVOCATIONS[event](subject), IndexingJob)
        return

    with pytest.raises(IllegalJobTransitionError):
        INVOCATIONS[event](subject)


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
)
def test_a_finished_job_cannot_be_cancelled(status: JobStatus) -> None:
    """RFC-029 section 8: this is an error, not a silent no-op.

    A caller asking to cancel a job that finished an hour ago is acting on
    a wrong belief, and a 202 would confirm it.
    """
    with pytest.raises(IllegalJobTransitionError):
        job(status).request_cancel()


def test_terminal_and_active_partition_the_states() -> None:
    """`is_active` is what the partial unique index covers (RFC-029 section 9)."""
    active = {status for status in JobStatus if status.is_active}
    terminal = {status for status in JobStatus if status.is_terminal}

    assert active == {JobStatus.PENDING, JobStatus.RUNNING}
    assert active | terminal == set(JobStatus)
    assert not active & terminal


def test_claiming_starts_the_clock_and_stamps_a_heartbeat() -> None:
    claimed = job(JobStatus.PENDING).claim(LATER)

    assert claimed.status is JobStatus.RUNNING
    assert claimed.started_at == LATER
    assert claimed.last_heartbeat_at == LATER


def test_reclaiming_a_resumed_job_keeps_the_original_start() -> None:
    """`created_at -> started_at` is the queue latency of RFC-029 section 6.1.

    Rewriting it on every claim would make a job that crashed twice look
    like it had started promptly.
    """
    resumed = job(JobStatus.PENDING, started_at=NOW, attempts=1)

    claimed = resumed.claim(LATER)

    assert claimed.started_at == NOW
    assert claimed.last_heartbeat_at == LATER


def test_an_expired_job_returns_to_the_queue_with_its_checkpoint() -> None:
    """The correction of RFC-029 section 9.1.

    The draft marked an abandoned job `failed` and left the user to
    recreate it -- which mints a new row with a NULL checkpoint, so the
    checkpoint column would never have had a reader at all.
    """
    checkpoint = ImagePath("fotos/2018/DSC_0100.JPG")
    running = job(
        JobStatus.RUNNING,
        started_at=NOW,
        last_heartbeat_at=NOW,
        last_processed_relative_path=checkpoint,
    )

    expired = running.expire(LATER, MAX_ATTEMPTS)

    assert expired.status is JobStatus.PENDING
    assert expired.last_processed_relative_path == checkpoint
    assert expired.attempts == 1
    assert expired.last_heartbeat_at is None
    assert expired.error_message is None


def test_expiring_past_the_attempt_limit_fails_the_job() -> None:
    """The bound that stops a process-killing file from looping for ever."""
    running = job(JobStatus.RUNNING, attempts=MAX_ATTEMPTS - 1, last_heartbeat_at=NOW)

    expired = running.expire(LATER, MAX_ATTEMPTS)

    assert expired.status is JobStatus.FAILED
    assert expired.attempts == MAX_ATTEMPTS
    assert expired.finished_at == LATER
    assert expired.error_message is not None


def test_expiring_a_job_the_user_cancelled_reports_the_cancellation() -> None:
    """Blaming the system for a stop the user chose would be the wrong record."""
    running = job(JobStatus.RUNNING, cancel_requested=True, last_heartbeat_at=NOW)

    expired = running.expire(LATER, MAX_ATTEMPTS)

    assert expired.status is JobStatus.CANCELLED
    assert expired.attempts == 0
    assert expired.finished_at == LATER


def test_a_clean_stop_costs_no_attempt() -> None:
    """`Ctrl+C` is an operator, not a crash (RFC-029 section 9.1)."""
    running = job(JobStatus.RUNNING, attempts=1, last_heartbeat_at=NOW)

    released = running.release(LATER)

    assert released.status is JobStatus.PENDING
    assert released.attempts == 1
    assert released.last_heartbeat_at is None


def test_failing_costs_no_attempt_either() -> None:
    """Nothing retries a failed job, so nothing has consumed a life."""
    failed = job(JobStatus.RUNNING, last_heartbeat_at=NOW).fail(LATER, "disk gone")

    assert failed.attempts == 0
    assert failed.error_message == "disk gone"
    assert failed.finished_at == LATER


def test_progress_is_not_a_transition() -> None:
    running = job(JobStatus.RUNNING, last_heartbeat_at=NOW)

    updated = running.with_progress(
        IndexingProgress(discovered_files=10, skipped_images=10),
        ImagePath("a/b.jpg"),
        LATER,
    )

    assert updated.status is JobStatus.RUNNING
    assert updated.progress.discovered_files == 10
    assert updated.last_processed_relative_path == ImagePath("a/b.jpg")
    assert updated.last_heartbeat_at == LATER


def test_a_checkpoint_of_none_leaves_the_previous_one_standing() -> None:
    """`None` means "nothing is durable yet", never "forget where we were".

    The pending buffer can hold the oldest undecided file for several
    windows, and clearing the checkpoint in that window would throw away a
    position a crash still needs.
    """
    checkpoint = ImagePath("a/b.jpg")
    running = job(
        JobStatus.RUNNING,
        last_heartbeat_at=NOW,
        last_processed_relative_path=checkpoint,
    )

    updated = running.with_progress(IndexingProgress(), None, LATER)

    assert updated.last_processed_relative_path == checkpoint


def test_jobs_compare_by_identity() -> None:
    """Two readings of one job at two moments are the same job (RFC-009)."""
    original = job(JobStatus.PENDING)
    claimed = original.claim(LATER)

    assert claimed == original
    assert len({original, claimed}) == 1


def test_scopes_default_to_the_whole_device() -> None:
    """Zero scopes is how RFC-029 section 5.2 spells "everything on the disk"."""
    assert job().scopes == ()
    assert job(scopes=(JobScope("2018"),)).scopes == (JobScope("2018"),)
