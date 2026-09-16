"""The two races RFC-029 is built on, run against two real connections.

**A concurrency test with one session tests nothing.** The `db_session`
fixture isolates every other integration test inside a SAVEPOINT, which
is exactly the wrong shape here: two claims from one session cannot
contend, and a check-then-act inside one transaction looks correct
because it *is* correct within one transaction. So these tests open their
own connections, commit for real, and clean up explicitly in a `finally`.

The two properties, and why each is invisible without this file:

* **`SKIP LOCKED`.** Without it, an executor that finds the row another
  executor is already claiming does not take the next job -- it either
  blocks or, under `READ COMMITTED`, re-evaluates the `WHERE`, matches
  nothing and goes back to sleep with work still queued. That is not
  corruption, so nothing fails; the queue is just slower than it looks,
  and only with more than one executor, which is never how tests run.
* **The partial unique index.** A Python `SELECT` before the `INSERT`
  passes in both sessions when they interleave, and both jobs are
  created. One session can never show that.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.domain.value_objects.job_id import JobId
from app.infrastructure.database.models.device_model import DeviceModel
from app.infrastructure.database.models.indexing_job_model import IndexingJobModel
from app.infrastructure.filesystem.device_identity import compute_device_id
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (
    PostgresIndexingJobRepository,
)
from app.infrastructure.persistence.session import SessionLocal

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(minutes=10)

CONCURRENCY_VOLUMES = [
    VolumeIdentity(
        value=f"\\\\?\\Volume{{{uuid.UUID(int=0xC0FFEE + index)}}}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )
    for index in range(2)
]
"""Volumes no machine has, so nothing here can collide with a real disk.

These rows are genuinely committed to the development database -- that is
the whole point -- so they are removed again in the fixture's teardown
rather than relying on a rollback that is deliberately absent.
"""

DEVICE_IDS = [compute_device_id(volume) for volume in CONCURRENCY_VOLUMES]


@pytest.fixture()
def committed_devices() -> Iterator[list[DeviceId]]:
    """Commit two throwaway devices, and take them away again afterwards.

    Teardown deletes the jobs before the devices, because
    `indexing_jobs.device_id` is a foreign key that refuses rather than
    cascades -- the same refusal RFC-027 section 6.3 wants for images.
    """
    session = SessionLocal()
    repository = PostgresDeviceRepository(session)
    try:
        for index, volume in enumerate(CONCURRENCY_VOLUMES):
            repository.save(
                Device(
                    id=DEVICE_IDS[index],
                    volume_identity=volume,
                    label=f"CONCURRENCY-TEST-{index}",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
        yield DEVICE_IDS
    finally:
        _cleanup(session)
        session.close()


def _cleanup(session: Session) -> None:
    """Remove only what this module created, never anything else."""
    device_values = [device_id.value for device_id in DEVICE_IDS]
    session.rollback()
    session.execute(
        delete(IndexingJobModel).where(IndexingJobModel.device_id.in_(device_values))
    )
    session.execute(delete(DeviceModel).where(DeviceModel.id.in_(device_values)))
    session.commit()


def queued_job(device_id: DeviceId, created_at: datetime.datetime) -> IndexingJob:
    return IndexingJob(id=JobId.new(), device_id=device_id, created_at=created_at)


def test_a_locked_job_is_skipped_rather_than_waited_on(
    committed_devices: list[DeviceId],
) -> None:
    """The `SKIP LOCKED` clause, made deterministic instead of raced.

    One session holds a row lock on the oldest queued job, exactly as a
    concurrent claim would for the instant it runs. The other executor
    must come away with the *second* job -- not `None`, and not a wait.

    `lock_timeout` is what turns a regression into a failure instead of a
    hang: an implementation without `SKIP LOCKED` blocks here, and this
    makes it block for two seconds and then raise.
    """
    first_device, second_device = committed_devices
    writer = SessionLocal()
    claimer = SessionLocal()
    try:
        repository = PostgresIndexingJobRepository(writer)
        older = repository.create(queued_job(first_device, NOW))
        newer = repository.create(queued_job(second_device, LATER))

        # Hold the lock the way a concurrent claim would, and keep holding
        # it while the other executor polls.
        locker = SessionLocal()
        try:
            locked_id = locker.execute(
                text(
                    "SELECT id FROM indexing_jobs WHERE status = 'pending' "
                    "ORDER BY created_at LIMIT 1 FOR UPDATE"
                )
            ).scalar_one()
            assert locked_id == older.id.value

            claimer.execute(text("SET lock_timeout = '2s'"))
            claimed = PostgresIndexingJobRepository(claimer).claim_next(LATER)
        finally:
            locker.rollback()
            locker.close()

        assert claimed is not None, "the second queued job was not claimed at all"
        assert claimed.id == newer.id
        assert claimed.status is JobStatus.RUNNING
    finally:
        claimer.close()
        _cleanup(writer)
        writer.close()


def test_two_concurrent_claims_take_two_different_jobs(
    committed_devices: list[DeviceId],
) -> None:
    """Two executors, two connections, two jobs -- never the same one twice."""
    first_device, second_device = committed_devices
    writer = SessionLocal()
    try:
        repository = PostgresIndexingJobRepository(writer)
        repository.create(queued_job(first_device, NOW))
        repository.create(queued_job(second_device, LATER))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_claim_in_a_fresh_session, [LATER, LATER]))

        claimed = [job for job in results if job is not None]
        assert len(claimed) == 2, "an executor came away empty with work queued"
        assert claimed[0].id != claimed[1].id
    finally:
        _cleanup(writer)
        writer.close()


def _claim_in_a_fresh_session(now: datetime.datetime) -> IndexingJob | None:
    """Claim from a connection of this thread's own, as an executor would."""
    session = SessionLocal()
    try:
        return PostgresIndexingJobRepository(session).claim_next(now)
    finally:
        session.close()


def test_two_concurrent_creations_for_one_device_leave_one_winner(
    committed_devices: list[DeviceId],
) -> None:
    """RFC-029 section 9, proven where a Python check could not hold.

    Both threads decide from a database in which the device has no active
    job. A check-then-act would let both through; the partial unique index
    refuses the second, and `PostgresIndexingJobRepository` turns that
    refusal into the domain error the route answers 409 with.
    """
    device_id = committed_devices[0]
    writer = SessionLocal()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(_create_in_a_fresh_session, [device_id] * 2))

        created = [outcome for outcome in outcomes if isinstance(outcome, IndexingJob)]
        refused = [
            outcome for outcome in outcomes if isinstance(outcome, DeviceBusyError)
        ]

        assert len(created) == 1
        assert len(refused) == 1
        assert (
            writer.execute(
                text("SELECT count(*) FROM indexing_jobs WHERE device_id = :device"),
                {"device": device_id.value},
            ).scalar_one()
            == 1
        )
    finally:
        _cleanup(writer)
        writer.close()


def _create_in_a_fresh_session(
    device_id: DeviceId,
) -> IndexingJob | DeviceBusyError:
    """Create from this thread's own connection, returning either outcome.

    The exception is returned rather than raised so the caller can assert
    on the pair of outcomes; which thread wins is genuinely undetermined,
    and only the *shape* of the result -- one of each -- is the contract.
    """
    session = SessionLocal()
    try:
        return PostgresIndexingJobRepository(session).create(queued_job(device_id, NOW))
    except DeviceBusyError as exc:
        return exc
    finally:
        session.close()
