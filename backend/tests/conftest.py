"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.infrastructure.database.models.image_model import ImageModel
from app.infrastructure.persistence.engine import EngineInstance


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Yield a PostgreSQL session isolated in a SAVEPOINT, rolled back on teardown.

    Infrastructure/integration tests that need a real database must depend on
    this fixture rather than opening a session directly. The session is bound
    to a connection-level transaction via `join_transaction_mode="create_savepoint"`,
    so any `session.commit()` or `session.rollback()` performed by the code
    under test (e.g. a repository translating an `IntegrityError`) only
    affects a SAVEPOINT nested inside that outer transaction. The outer
    transaction itself is always rolled back in the `finally` block below,
    so no row written during a test is ever visible outside of it, and the
    development database is left unmodified regardless of test outcome.
    """
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def empty_db_session(db_session: Session) -> Session:
    """Yield a `db_session` whose `images` table is empty for the test.

    Search tests are top-K queries over the whole table, so unlike the
    indexing tests they cannot defend themselves by asserting on specific
    ids: a row committed to the shared development database by unrelated
    work can place itself between the expected results and destroy the
    measurement, and a recall number computed over a table with extra
    rows in it is simply wrong rather than noisy.

    The DELETE runs inside the outer transaction `db_session` already
    opened and is undone by the rollback in its teardown, so this costs no
    new infrastructure and leaves the development database untouched. It
    is committed at the session level first so that a later
    `session.rollback()` inside the code under test (which rolls back to
    the current SAVEPOINT) cannot resurrect the rows.

    **Caveat, deliberately accepted (RFC-025 section 8.1).** This holds a
    write lock on every existing row for the duration of the test and does
    not protect against a *concurrent* writer -- another process would
    block, and its rows would appear if it committed first. That is
    acceptable because the suite runs in one process: `requirements.txt`
    pins `pytest` and `pytest-mock` and no `pytest-xdist`. A dedicated
    test database would remove the caveat and is future work: the engine
    is a module-level singleton built from `settings.database_url` at
    import time, there is one `.env` and one `POSTGRES_DB` in
    `docker-compose.yml`, and a fresh database would need
    `CREATE EXTENSION vector` plus every migration before the first test.
    """
    db_session.execute(delete(ImageModel))
    db_session.commit()
    return db_session
