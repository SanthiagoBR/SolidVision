"""RFC-046's migration against a real server: `images.missing_since`.

A cluster of its own, in the test's temporary directory -- never the
development database. A row is written at the revision before RFC-046; the
upgrade must give it the new column, NULL -- present -- and the downgrade must
take the column away and leave the row.

Marked `embedded` and deselected by default: `pytest -m embedded`.
"""

from __future__ import annotations

import datetime
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine.interfaces import ReflectedColumn
from sqlalchemy.pool import NullPool
from tests.conftest import make_test_device

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

pytestmark = [
    pytest.mark.embedded,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="The embedded cluster is Windows-only (RFC-033 section 4.1)",
    ),
]

BEFORE_RFC_046 = "c3f81a5e7d24"
RFC_046 = "d2a7e4c19b53"


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


def columns(db: Connection) -> dict[str, ReflectedColumn]:
    return {column["name"]: column for column in inspect(db).get_columns("images")}


def test_the_column_comes_null_and_goes_back_out(database: ClusterConnection) -> None:
    engine = create_engine(database.url(), poolclass=NullPool)
    device = make_test_device(label="HD2")
    now = datetime.datetime.now(tz=datetime.UTC)
    try:
        with engine.connect() as db:
            migrate(db, BEFORE_RFC_046)
            db.execute(
                text(
                    "INSERT INTO devices (id, volume_identity, volume_kind, label, "
                    "first_seen_at, last_seen_at) VALUES (:id, :identity, :kind, "
                    "'HD2', :now, :now)"
                ),
                {
                    "id": device.id.value,
                    "identity": device.volume_identity.value,
                    "kind": device.volume_identity.kind.value,
                    "now": now,
                },
            )
            db.execute(
                text(
                    "INSERT INTO images (id, device_id, relative_path, filename, "
                    "extension) VALUES (gen_random_uuid(), :device, "
                    "'fotos/x.jpg', 'x', 'jpg')"
                ),
                {"device": device.id.value},
            )
            db.commit()
            assert "missing_since" not in columns(db)

            migrate(db, RFC_046)

            column = columns(db)["missing_since"]
            assert column["nullable"] is True
            assert "TIMESTAMP" in str(column["type"]).upper()
            assert getattr(column["type"], "timezone", None) is True
            assert (
                db.execute(
                    text("SELECT count(*) FROM images WHERE missing_since IS NULL")
                ).scalar_one()
                == 1
            )

            migrate(db, BEFORE_RFC_046, down=True)

            assert "missing_since" not in columns(db)
            assert db.execute(text("SELECT count(*) FROM images")).scalar_one() == 1
    finally:
        engine.dispose()
