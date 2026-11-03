"""A job the CLI creates and claims in one session reads back as it stands.

The defect RFC-045's measurement found in RFC-029's repository: the CLI's
session is made with `expire_on_commit=False`, so the model `create()` wrote
-- `pending` -- stays in the session's identity map for as long as it is
alive, and the cycle between a job and its scopes keeps it alive until the
garbage collector runs. Without `populate_existing`, the claim, the first
`record_progress()` and the compare-and-set of `save_if_status()` read that
copy instead of the row: the job looked taken over, the CLI followed it for
ever, and the row stayed `running`.

Whether it happened depended on the collector, so these tests switch it off:
the copy then survives every time, as it did in the run that found it.
"""

from __future__ import annotations

import datetime
import gc
from collections.abc import Iterator

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.database.models.indexing_job_model import IndexingJobModel
from app.infrastructure.persistence.postgres_indexing_job_repository import (
    PostgresIndexingJobRepository,
)

NOW = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture()
def cli_session(db_session: Session) -> Iterator[Session]:
    """A session made as `SessionLocal` makes one, in the test's transaction."""
    db_session.execute(delete(IndexingJobModel))
    db_session.commit()
    session = Session(
        bind=db_session.bind,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    collecting = gc.isenabled()
    gc.disable()
    try:
        yield session
    finally:
        if collecting:
            gc.enable()
        session.close()


def queued(scopes: tuple[str, ...] = ("fotos/2018",)) -> IndexingJob:
    return IndexingJob(
        id=JobId.new(),
        device_id=TEST_DEVICE_ID,
        scopes=tuple(JobScope(scope) for scope in scopes),
        created_at=NOW,
    )


def test_the_claim_reads_running_not_the_pending_create_left(
    cli_session: Session,
) -> None:
    jobs = PostgresIndexingJobRepository(cli_session)
    created = jobs.create(queued())

    claimed = jobs.claim(created.id, NOW)

    assert claimed is not None
    assert claimed.status is JobStatus.RUNNING


def test_the_first_progress_of_a_claimed_job_is_written(
    cli_session: Session,
) -> None:
    """The write that used to answer `None` -- "taken over" -- and stop the run."""
    jobs = PostgresIndexingJobRepository(cli_session)
    created = jobs.create(queued())
    jobs.claim(created.id, NOW)

    stored = jobs.record_progress(
        created.id, IndexingProgress(discovered_files=3), None, NOW
    )

    assert stored is not None
    assert stored.status is JobStatus.RUNNING
    assert stored.progress.discovered_files == 3


def test_a_claimed_job_can_be_finished_by_the_session_that_created_it(
    cli_session: Session,
) -> None:
    """`JobRunner._finish()`: a `get()` and a compare-and-set on `running`."""
    jobs = PostgresIndexingJobRepository(cli_session)
    created = jobs.create(queued(scopes=()))
    jobs.claim(created.id, NOW)

    current = jobs.get(created.id)
    assert current is not None
    assert current.status is JobStatus.RUNNING
    finished = jobs.save_if_status(current.complete(NOW), JobStatus.RUNNING)

    assert finished is not None
    assert finished.status is JobStatus.COMPLETED
