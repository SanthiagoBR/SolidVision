"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

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
