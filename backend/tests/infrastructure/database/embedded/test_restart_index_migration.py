"""RFC-045 section 5.9 against a real server: the restart of the index.

A cluster of its own, in the test's temporary directory -- never the
development database. The schema is brought to the revision before RFC-045,
filled with a device, an indexed image and a job with a scope, and then
migrated: the images and the jobs must go, and the device must stay as it
was. The downgrade must leave the schema at the previous revision, and the
upgrade after it must not fail.

Marked `embedded` and deselected by default, like the other tests that start
real servers: `pytest -m embedded`.
"""

from __future__ import annotations

import datetime
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from tests.conftest import make_test_device

from app.domain.entities.indexing_job import IndexingJob
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.database_setup import (
    ALEMBIC_INI,
    ALEMBIC_SCRIPTS,
    ClusterConnection,
    ensure_database,
)
from app.infrastructure.database.embedded.errors import RuntimeInvalidError
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (
    PostgresIndexingJobRepository,
)

pytestmark = [
    pytest.mark.embedded,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
    ),
]

BEFORE_RFC_045 = "6d77379a36a1"
RFC_045 = "c3f81a5e7d24"


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[ClusterConnection]:
    runtime = PostgresRuntime(settings.embedded_postgres_runtime_directory)
    try:
        runtime.manifest()
    except RuntimeInvalidError as error:
        pytest.skip(str(error))
    cluster = EmbeddedPostgres(
        runtime, ClusterHome(tmp_path / "SolidVision" / "postgres")
    )
    started = cluster.start()
    try:
        ensure_database(started.connection)
        yield started.connection
    finally:
        cluster.stop()


def migrate(db: Connection, revision: str, *, down: bool = False) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    config.attributes["connection"] = db
    if down:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)
    db.commit()


def count(db: Connection, table: str) -> int:
    return int(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_the_index_and_the_jobs_go_and_the_devices_stay(
    database: ClusterConnection,
) -> None:
    engine = create_engine(database.url(), poolclass=NullPool)
    device = make_test_device(label="HD2")
    now = datetime.datetime.now(tz=datetime.UTC)
    try:
        with engine.connect() as db:
            migrate(db, BEFORE_RFC_045)
            with Session(bind=db) as session:
                PostgresDeviceRepository(session).save(device)
                # In SQL, not through `PostgresImageRepository`: the ORM
                # model is the head's, and since RFC-046 it reads a column
                # this revision does not have yet.
                path = ImagePath("fotos/2018/DJI_0042.JPG")
                session.execute(
                    text(
                        "INSERT INTO images (id, device_id, relative_path, "
                        "filename, extension, file_size, file_modified_at, "
                        "content_hash) VALUES (:id, :device, :path, 'DJI_0042', "
                        "'jpg', 1024, :now, :hash)"
                    ),
                    {
                        "id": compute_image_id(device.id, path).value,
                        "device": device.id.value,
                        "path": str(path),
                        "now": now,
                        "hash": "a" * 64,
                    },
                )
                PostgresIndexingJobRepository(session).create(
                    IndexingJob(
                        id=JobId.new(),
                        device_id=device.id,
                        scopes=(JobScope("fotos/2018"),),
                        created_at=now,
                    )
                )
                session.commit()
            assert [
                count(db, table)
                for table in ("images", "indexing_jobs", "indexing_job_scopes")
            ] == [1, 1, 1]

            migrate(db, RFC_045)

            assert count(db, "images") == 0
            assert count(db, "indexing_jobs") == 0
            assert count(db, "indexing_job_scopes") == 0
            assert db.execute(
                text("SELECT id, label, volume_identity FROM devices")
            ).all() == [(device.id.value, "HD2", device.volume_identity.value)]
            assert _revision(db) == RFC_045

            migrate(db, BEFORE_RFC_045, down=True)
            assert _revision(db) == BEFORE_RFC_045
            assert count(db, "devices") == 1

            migrate(db, RFC_045)
            assert _revision(db) == RFC_045
    finally:
        engine.dispose()


def _revision(db: Connection) -> str:
    return str(db.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
