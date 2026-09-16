"""One contract test for `IndexingJobRepository`, two implementations (RFC-029).

Written once and run against `PostgresIndexingJobRepository` and
`InMemoryIndexingJobRepository`, for the reason
`test_device_repository_contract.py` gives: a double that stores or
refuses slightly differently from PostgreSQL turns every green test above
it into evidence about the double.

Two clauses of the contract matter more than the rest here, and both are
about refusal rather than storage. **One active job per device** is what
stops two runs fighting over one disk head, and the double has to enforce
it even though it has no index to lean on. **Claiming is atomic** is what
the polling loop is built on, and the double has to make it so even
though it has no row locks.

Comparisons use `state()` rather than `==`. `IndexingJob` compares by id,
following `Image` and `Device`, so asserting `claimed == job` would pass
against an implementation that stored nothing at all.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.database.models.indexing_job_model import IndexingJobModel
from app.infrastructure.filesystem.device_identity import compute_device_id
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (
    PostgresIndexingJobRepository,
)

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(minutes=10)
MAX_ATTEMPTS = 3

SECOND_VOLUME = VolumeIdentity(
    value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000fe}\\",
    kind=VolumeKind.WINDOWS_VOLUME_GUID,
)
SECOND_DEVICE_ID: DeviceId = compute_device_id(SECOND_VOLUME)
"""A second disk, so "one active job per device" can be told from "one job".

Derived exactly as production derives it, like `TEST_DEVICE_ID`: a
hand-picked UUID would put these jobs on a device no real volume could
produce.
"""


@pytest.fixture(params=["postgres", "in_memory"])
def repository(request: pytest.FixtureRequest) -> Iterator[IndexingJobRepository]:
    """Yield each `IndexingJobRepository` implementation in turn.

    The PostgreSQL round empties `indexing_jobs` and registers the second
    device, because several assertions below are about what `list()`
    contains and cannot be written against a table somebody else already
    put a row in. Scope rows go with their jobs through `ON DELETE
    CASCADE`, so deleting the parents is enough.
    """
    if request.param == "postgres":
        session = request.getfixturevalue("db_session")
        session.execute(delete(IndexingJobModel))
        _register_second_device(session)
        session.commit()
        yield PostgresIndexingJobRepository(session)
    else:
        yield InMemoryIndexingJobRepository()


def _register_second_device(session: Session) -> None:
    """Give the second device a row, so the foreign key has something to hit."""
    PostgresDeviceRepository(session).save(
        Device(
            id=SECOND_DEVICE_ID,
            volume_identity=SECOND_VOLUME,
            label="SECOND-TEST-DEVICE",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )


def job(
    device_id: DeviceId = TEST_DEVICE_ID,
    created_at: datetime.datetime = NOW,
    **overrides: object,
) -> IndexingJob:
    """Build a queued job for a device that exists."""
    fields: dict[str, object] = {
        "id": JobId.new(),
        "device_id": device_id,
        "created_at": created_at,
    }
    fields.update(overrides)
    return IndexingJob(**fields)  # type: ignore[arg-type]


def state(job_value: IndexingJob) -> dict[str, Any]:
    """Every field of a job, for comparisons `==` deliberately cannot make.

    `IndexingJob.__eq__` is identity-based (RFC-009), which is right for a
    set of jobs and useless for "did this round trip intact".
    """
    return dataclasses.asdict(job_value)


def test_a_created_job_is_retrievable(repository: IndexingJobRepository) -> None:
    created = repository.create(job())

    assert state(repository.get(created.id) or created) == state(created)


def test_an_unknown_id_returns_none(repository: IndexingJobRepository) -> None:
    assert repository.get(JobId(uuid.uuid4())) is None


def test_scopes_survive_the_round_trip(repository: IndexingJobRepository) -> None:
    scopes = (JobScope("2018"), JobScope("2019/janeiro"))

    created = repository.create(job(scopes=scopes))

    stored = repository.get(created.id)
    assert stored is not None
    assert stored.scopes == scopes


def test_no_scopes_means_the_whole_device(repository: IndexingJobRepository) -> None:
    """Zero rows is how RFC-029 section 5.2 spells "everything on the disk"."""
    created = repository.create(job(scopes=()))

    stored = repository.get(created.id)
    assert stored is not None
    assert stored.scopes == ()


def test_a_second_active_job_for_one_device_is_refused(
    repository: IndexingJobRepository,
) -> None:
    """RFC-029 section 9, and the clause the in-memory double must emulate.

    Two runs over one disk duplicate inference over the overlap and fight
    for the same read head, which is the slowest resource involved.
    """
    repository.create(job())

    with pytest.raises(DeviceBusyError):
        repository.create(job())


def test_a_running_job_also_holds_the_device(
    repository: IndexingJobRepository,
) -> None:
    created = repository.create(job())
    repository.save_if_status(created.claim(NOW), JobStatus.PENDING)

    with pytest.raises(DeviceBusyError):
        repository.create(job())


def test_another_device_is_free_to_start(repository: IndexingJobRepository) -> None:
    """The index is per device, not global: two disks index in parallel."""
    repository.create(job())

    other = repository.create(job(device_id=SECOND_DEVICE_ID))

    assert other.device_id == SECOND_DEVICE_ID


@pytest.mark.parametrize(
    "finish",
    [
        lambda j: j.claim(NOW).complete(LATER),
        lambda j: j.claim(NOW).fail(LATER, "boom"),
        lambda j: j.cancel(LATER),
    ],
)
def test_a_device_accepts_a_new_job_once_the_old_one_ends(
    repository: IndexingJobRepository,
    finish: Any,
) -> None:
    """Every terminal state releases the disk, not just the happy one."""
    created = repository.create(job())
    repository.save_if_status(finish(created), JobStatus.PENDING)

    assert repository.create(job()).status is JobStatus.PENDING


def test_claiming_takes_the_oldest_queued_job(
    repository: IndexingJobRepository,
) -> None:
    """Oldest first by `created_at`; there is no priority (RFC-029 section 14)."""
    older = repository.create(job(created_at=NOW))
    repository.create(job(device_id=SECOND_DEVICE_ID, created_at=LATER))

    claimed = repository.claim_next(LATER)

    assert claimed is not None
    assert claimed.id == older.id


def test_a_claim_is_exactly_the_domain_transition(
    repository: IndexingJobRepository,
) -> None:
    """The guard on the one place the SQL restates a domain rule.

    An atomic claim has to be a single statement, so `claim_next()` cannot
    compute the transition in Python from a row it read first -- it writes
    the same move in SQL. This is what stops the two from drifting.
    """
    created = repository.create(job())

    claimed = repository.claim_next(LATER)

    assert claimed is not None
    assert state(claimed) == state(created.claim(LATER))


def test_claiming_an_empty_queue_returns_none(
    repository: IndexingJobRepository,
) -> None:
    assert repository.claim_next(NOW) is None


def test_a_claimed_job_is_not_claimed_twice(
    repository: IndexingJobRepository,
) -> None:
    repository.create(job())

    first = repository.claim_next(NOW)
    second = repository.claim_next(NOW)

    assert first is not None
    assert second is None


def test_two_queued_jobs_are_claimed_one_each(
    repository: IndexingJobRepository,
) -> None:
    repository.create(job(created_at=NOW))
    repository.create(job(device_id=SECOND_DEVICE_ID, created_at=LATER))

    first = repository.claim_next(LATER)
    second = repository.claim_next(LATER)

    assert first is not None and second is not None
    assert first.id != second.id


def test_a_resumed_job_keeps_the_moment_it_first_started(
    repository: IndexingJobRepository,
) -> None:
    """`created_at -> started_at` is the queue latency of RFC-029 section 6.1."""
    created = repository.create(job())
    repository.save_if_status(
        created.claim(NOW).expire(LATER, MAX_ATTEMPTS), JobStatus.PENDING
    )

    reclaimed = repository.claim_next(LATER + datetime.timedelta(minutes=5))

    assert reclaimed is not None
    assert reclaimed.started_at == NOW
    assert reclaimed.attempts == 1


def test_claiming_a_named_job_takes_that_one(
    repository: IndexingJobRepository,
) -> None:
    """What the CLI does: create a job, then run it in its own process."""
    created = repository.create(job())

    claimed = repository.claim(created.id, LATER)

    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.status is JobStatus.RUNNING


def test_claiming_a_named_job_someone_else_took_returns_none(
    repository: IndexingJobRepository,
) -> None:
    """The CLI has to lose gracefully and follow the job by polling instead."""
    created = repository.create(job())
    repository.claim_next(NOW)

    assert repository.claim(created.id, LATER) is None


def test_claiming_an_unknown_job_returns_none(
    repository: IndexingJobRepository,
) -> None:
    assert repository.claim(JobId(uuid.uuid4()), NOW) is None


def test_progress_and_checkpoint_are_stored(
    repository: IndexingJobRepository,
) -> None:
    created = repository.create(job())
    repository.claim(created.id, NOW)

    written = repository.record_progress(
        created.id,
        IndexingProgress(
            discovered_files=512,
            processed_images=100,
            skipped_images=400,
            failed_images=12,
            discovery_complete=True,
        ),
        ImagePath("fotos/2018/DSC_0100.JPG"),
        LATER,
    )

    assert written is not None
    stored = repository.get(created.id)
    assert stored is not None
    assert stored.progress.discovered_files == 512
    assert stored.progress.skipped_images == 400
    assert stored.progress.discovery_complete is True
    assert stored.last_processed_relative_path == ImagePath("fotos/2018/DSC_0100.JPG")
    assert stored.last_heartbeat_at == LATER


def test_progress_does_not_overwrite_a_cancellation(
    repository: IndexingJobRepository,
) -> None:
    """The bug that would make a job impossible to cancel (RFC-029 section 8).

    The worker's copy of the job was read when it claimed the row and says
    `cancel_requested = False`. A heartbeat that wrote the whole entity
    would put that stale `False` over a cancellation the route recorded
    since -- and the job would simply never stop, with nothing anywhere
    looking wrong.
    """
    created = repository.create(job())
    running = repository.claim(created.id, NOW)
    assert running is not None
    repository.save_if_status(running.request_cancel(), JobStatus.RUNNING)

    written = repository.record_progress(
        created.id, IndexingProgress(processed_images=8), None, LATER
    )

    assert written is not None
    assert written.cancel_requested is True


def test_progress_reports_back_that_the_job_was_taken_away(
    repository: IndexingJobRepository,
) -> None:
    """How a worker finds out the reaper requeued the job it is still running."""
    created = repository.create(job())
    running = repository.claim(created.id, NOW)
    assert running is not None
    repository.save_if_status(running.expire(LATER, MAX_ATTEMPTS), JobStatus.RUNNING)

    assert (
        repository.record_progress(created.id, IndexingProgress(), None, LATER) is None
    )


def test_progress_on_an_unknown_job_reports_nothing(
    repository: IndexingJobRepository,
) -> None:
    assert (
        repository.record_progress(JobId(uuid.uuid4()), IndexingProgress(), None, LATER)
        is None
    )


def test_a_null_checkpoint_leaves_the_stored_one_alone(
    repository: IndexingJobRepository,
) -> None:
    """`None` means "nothing new is durable", never "forget where we were"."""
    created = repository.create(job())
    repository.claim(created.id, NOW)
    repository.record_progress(
        created.id, IndexingProgress(), ImagePath("a/b.jpg"), NOW
    )

    repository.record_progress(created.id, IndexingProgress(), None, LATER)

    stored = repository.get(created.id)
    assert stored is not None
    assert stored.last_processed_relative_path == ImagePath("a/b.jpg")


def test_a_conditional_write_lands_while_the_status_still_matches(
    repository: IndexingJobRepository,
) -> None:
    created = repository.create(job())

    written = repository.save_if_status(created.cancel(LATER), JobStatus.PENDING)

    assert written is not None
    stored = repository.get(created.id)
    assert stored is not None
    assert stored.status is JobStatus.CANCELLED


def test_a_conditional_write_is_dropped_once_the_status_moved(
    repository: IndexingJobRepository,
) -> None:
    """The race cancellation has to survive (RFC-029 section 8).

    A worker claims the job between the route's read and its write; the
    conditional write matches no row, and the caller falls back to setting
    the flag rather than overwriting a running job with a cancelled one.
    """
    created = repository.create(job())
    repository.claim_next(NOW)

    assert repository.save_if_status(created.cancel(LATER), JobStatus.PENDING) is None

    stored = repository.get(created.id)
    assert stored is not None
    assert stored.status is JobStatus.RUNNING


def test_a_conditional_write_to_an_unknown_job_returns_none(
    repository: IndexingJobRepository,
) -> None:
    assert repository.save_if_status(job(), JobStatus.PENDING) is None


def test_stale_jobs_are_the_running_ones_whose_heartbeat_stopped(
    repository: IndexingJobRepository,
) -> None:
    created = repository.create(job())
    repository.save_if_status(created.claim(NOW), JobStatus.PENDING)

    stale = repository.list_stale(NOW + datetime.timedelta(minutes=1))

    assert [found.id for found in stale] == [created.id]


def test_a_fresh_heartbeat_is_not_stale(repository: IndexingJobRepository) -> None:
    created = repository.create(job())
    repository.save_if_status(created.claim(LATER), JobStatus.PENDING)

    assert repository.list_stale(NOW) == []


def test_a_queued_job_is_never_stale(repository: IndexingJobRepository) -> None:
    """A pending job has no heartbeat to miss; only a claimed one can go quiet."""
    repository.create(job())

    assert repository.list_stale(LATER) == []


def test_a_finished_job_is_never_stale(repository: IndexingJobRepository) -> None:
    created = repository.create(job())
    repository.save_if_status(created.claim(NOW).complete(NOW), JobStatus.PENDING)

    assert repository.list_stale(LATER) == []


def test_listing_returns_newest_first(repository: IndexingJobRepository) -> None:
    older = repository.create(job(created_at=NOW))
    newer = repository.create(job(device_id=SECOND_DEVICE_ID, created_at=LATER))

    assert [found.id for found in repository.list()] == [newer.id, older.id]


def test_listing_filters_by_device(repository: IndexingJobRepository) -> None:
    mine = repository.create(job())
    repository.create(job(device_id=SECOND_DEVICE_ID))

    found = repository.list(device_id=TEST_DEVICE_ID)

    assert [entry.id for entry in found] == [mine.id]


def test_listing_filters_by_status(repository: IndexingJobRepository) -> None:
    queued = repository.create(job())
    running = repository.create(job(device_id=SECOND_DEVICE_ID))
    repository.save_if_status(running.claim(NOW), JobStatus.PENDING)

    found = repository.list(status=JobStatus.PENDING)

    assert [entry.id for entry in found] == [queued.id]


def test_listing_filters_by_both(repository: IndexingJobRepository) -> None:
    created = repository.create(job())
    repository.create(job(device_id=SECOND_DEVICE_ID))

    found = repository.list(device_id=TEST_DEVICE_ID, status=JobStatus.PENDING)

    assert [entry.id for entry in found] == [created.id]


def test_listing_an_empty_table_is_empty(repository: IndexingJobRepository) -> None:
    assert repository.list() == []
