"""Shared pytest fixtures for the backend test suite."""

from __future__ import annotations

import datetime
from collections.abc import Generator

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.infrastructure.database.models.device_model import DeviceModel
from app.infrastructure.database.models.image_model import ImageModel
from app.infrastructure.filesystem.device_identity import compute_device_id
from app.infrastructure.persistence.engine import EngineInstance
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)

TEST_VOLUME_IDENTITY = VolumeIdentity(
    value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000ff}\\",
    kind=VolumeKind.WINDOWS_VOLUME_GUID,
)
"""A volume no machine has, so no test can accidentally match a real disk.

Shaped like a genuine Windows volume GUID name rather than a placeholder
string, because `VolumeKind` says that is what it is and a value that
could not have come from `GetVolumeNameForVolumeMountPoint` would make the
fixtures describe a device the system could never produce.
"""

TEST_DEVICE_ID: DeviceId = compute_device_id(TEST_VOLUME_IDENTITY)
"""Derived, never hand-picked.

`ImageId` is `uuid5` over `f"{device_id}/{relative_path}"` since RFC-027,
so a literal UUID here would put every test image in a namespace that no
real device could occupy -- and the identity tests would then be checking
arithmetic rather than the derivation the product uses.
"""


def make_test_device(label: str = "TEST-DEVICE") -> Device:
    """Build the device every test image belongs to."""
    now = datetime.datetime.now(tz=datetime.UTC)
    return Device(
        id=TEST_DEVICE_ID,
        volume_identity=TEST_VOLUME_IDENTITY,
        label=label,
        first_seen_at=now,
        last_seen_at=now,
    )


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

    Since RFC-027 the session also arrives with the test device already
    saved. `images.device_id` is a NOT NULL foreign key, so every test that
    writes an image needs a device row to point at, and making each of them
    create one would be the same four lines repeated across a dozen files
    -- each free to invent a different device, which is exactly what the
    device-filtered search tests must be able to rely on not happening.
    """
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        PostgresDeviceRepository(session).save(make_test_device())
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

    Devices are emptied too, and in that order: `images.device_id` is a
    foreign key, so clearing devices first would be refused -- which is
    the refusal RFC-027 wants, since an image row is an embedding that
    cost real inference time. The test device is then put back, because
    every image the test goes on to write needs something to point at.

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
    db_session.execute(delete(DeviceModel))
    db_session.commit()
    PostgresDeviceRepository(db_session).save(make_test_device())
    return db_session
