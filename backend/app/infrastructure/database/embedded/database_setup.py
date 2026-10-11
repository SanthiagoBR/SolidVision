"""The database inside the cluster: create, migrate, check (RFC-033 section 7.4).

Three steps, in this order, all idempotent:

1. **The database.** `initdb` creates only `postgres`, `template0` and
   `template1`. `solidvision` is created from `template0` with the encoding
   and the locale spelled out, so nothing depends on the state of
   `template1`.
2. **The migrations.** The heads in `alembic_version` against the heads of
   the project's `ScriptDirectory`. Equal: nothing to do. Behind: `alembic
   upgrade head` in this process, on this connection, handed over in
   `config.attributes["connection"]`. A revision the code does not know:
   **refuse** -- an older app opening a database a newer one migrated.
3. **The facts.** The major version, `UTF8`, `C` and `C`, and `vector` at the
   manifest's version. A divergence names its field. An older `vector` than
   the runtime's is a warning: `ALTER EXTENSION vector UPDATE` belongs to
   upgrades, which are out of scope.

`CREATE EXTENSION vector` is not a step here. Migration `999b801e80f4`
(RFC-018) already runs it, and a second place would have to be kept in step
with the migration chain (section 2.3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from .errors import ClusterRefusedError, ServerNotReadyError, StartupFailedError

LOOPBACK = "127.0.0.1"
"""The literal, not `localhost`: the server listens on IPv4 only, and the
literal takes name resolution out of every connection (section 4.2)."""

SUPERUSER = "solidvision"
DATABASE = "solidvision"
EXPECTED_ENCODING = "UTF8"
EXPECTED_LOCALE = "C"

BACKEND_DIRECTORY = Path(__file__).resolve().parents[4]
ALEMBIC_INI = BACKEND_DIRECTORY / "alembic.ini"
ALEMBIC_SCRIPTS = BACKEND_DIRECTORY / "alembic"

MIGRATION_LOCK_KEY = 0x5356_3033
"""`pg_advisory_xact_lock` key ("SV03"): two callers migrating at once wait."""

ENVIRONMENT_VARIABLES = {
    "DATABASE_HOST": "host",
    "DATABASE_PORT": "port",
    "DATABASE_USER": "user",
    "DATABASE_PASSWORD": "password",
    "DATABASE_NAME": "database",
}
"""The bridge until RFC-034 (section 11), named in one place.

A test checks that every name is a field of `Settings`. A misspelt one would
make `Settings` fall back to the `.env`, which points at Docker, and the
suite would pass against the wrong database printing the same `passed`
(RFC-033a section 4.2).
"""

LIBPQ_OVERRIDES = {"options": "", "sslmode": "disable", "gssencmode": "disable"}
"""Explicit, so that `PGOPTIONS`, `PGSSLMODE` or `PGGSSENCMODE` in the
caller's environment cannot change the adapter's own connections. libpq falls
back to the environment only for a parameter that was not given at all."""

CONNECT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ClusterConnection:
    """Everything a client needs to reach the cluster. The password stays out
    of `repr`, and so out of every log line and traceback that prints one."""

    port: int
    password: str = field(repr=False)
    host: str = LOOPBACK
    user: str = SUPERUSER
    database: str = DATABASE

    def environment(self) -> dict[str, str]:
        """`DATABASE_*` for a child process: above the `.env` (RFC-004)."""
        return {
            name: str(getattr(self, attribute))
            for name, attribute in ENVIRONMENT_VARIABLES.items()
        }

    def url(self, database: str | None = None) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database or self.database,
        )

    def conninfo(self, database: str | None = None) -> str:
        """A libpq connection string for `psycopg.connect`, overrides included.

        It carries the password: build it where it is used, and do not log it.
        """
        return make_conninfo(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=database or self.database,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            **LIBPQ_OVERRIDES,
        )


class HeadComparison(Enum):
    CURRENT = "current"
    BEHIND = "behind"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PreparedDatabase:
    """What `prepare()` found and did."""

    created_database: bool
    migrated_from: tuple[str, ...] | None
    """The heads before the upgrade, or `None` when nothing ran."""
    heads: tuple[str, ...]
    warnings: tuple[str, ...]


def compare_heads(
    database_heads: frozenset[str],
    code_heads: frozenset[str],
    known_revisions: frozenset[str],
) -> HeadComparison:
    """Section 7.4, step 2. A database with no revision at all is behind."""
    if database_heads == code_heads:
        return HeadComparison.CURRENT
    if database_heads - known_revisions:
        return HeadComparison.UNKNOWN
    return HeadComparison.BEHIND


NOT_READY_MESSAGES = (
    "the database system is starting up",
    "the database system is in recovery mode",
    "the database system is not yet accepting connections",
    "the database system is not accepting connections",
)
"""What a live server answers with `cannot_connect_now` (SQLSTATE 57P03).

Matched by text because psycopg gives no SQLSTATE for a refusal at connection
time, and the text is stable for the reason section 8.1 gives: this build
has no translations.
"""


def verify_login(connection: ClusterConnection) -> None:
    """Connect with the password, or say which of three things went wrong.

    A refused password means the password-cluster pair is lost, and the
    adapter refuses rather than trying anything else (section 8.4). A server
    that says it cannot take connections *yet* is the one case worth
    retrying. Any other failure is the server not answering.
    """
    try:
        with psycopg.connect(connection.conninfo("postgres")):
            return
    except psycopg.OperationalError as error:
        text = str(error)
        if error.sqlstate == "28P01" or "password authentication failed" in text:
            raise ClusterRefusedError(
                f"The cluster on port {connection.port} rejected the password in "
                "the password file: the pair is lost. Not trying anything else."
            ) from None
        if error.sqlstate == "57P03" or any(m in text for m in NOT_READY_MESSAGES):
            raise ServerNotReadyError(
                f"The cluster on port {connection.port} is not taking connections "
                f"yet: {text.strip().splitlines()[-1]}"
            ) from None
        raise StartupFailedError(
            f"The cluster on port {connection.port} did not accept a connection: "
            f"{str(error).strip()}"
        ) from error


def _engine(connection: ClusterConnection, database: str | None = None) -> Engine:
    return create_engine(
        connection.url(database),
        poolclass=NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS, **LIBPQ_OVERRIDES},
    )


def _alembic_config() -> Any:
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    return config


def ensure_database(connection: ClusterConnection) -> bool:
    """`CREATE DATABASE` when `pg_database` lacks it. `True` when created."""
    engine = _engine(connection, "postgres")
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            exists = admin.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": connection.database},
            ).scalar()
            if exists:
                return False
            try:
                admin.execute(
                    text(
                        f'CREATE DATABASE "{connection.database}" TEMPLATE template0 '
                        f"ENCODING '{EXPECTED_ENCODING}' LOCALE '{EXPECTED_LOCALE}'"
                    )
                )
            except DBAPIError as error:
                # Another caller created it between the SELECT and here.
                if getattr(error.orig, "sqlstate", None) == "42P04":
                    return False
                raise
            return True
    finally:
        engine.dispose()


def database_heads(db: Connection) -> frozenset[str]:
    from alembic.runtime.migration import MigrationContext

    return frozenset(MigrationContext.configure(db).get_current_heads())


def code_revisions() -> tuple[frozenset[str], frozenset[str]]:
    """The code's heads, and every revision the code knows."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    heads = frozenset(script.get_heads())
    known = frozenset(revision.revision for revision in script.walk_revisions())
    return heads, known


def upgrade(db: Connection) -> None:
    """`alembic upgrade head`, in this process, on this connection."""
    from alembic import command

    config = _alembic_config()
    config.attributes["connection"] = db
    command.upgrade(config, "head")


def check_facts(db: Connection, major: int, pgvector: str) -> tuple[str, ...]:
    """Section 7.4, step 3: refuse a divergence, warn about an older `vector`."""
    row = db.execute(
        text(
            "SELECT current_setting('server_version_num')::int / 10000, "
            "pg_encoding_to_char(d.encoding), d.datcollate, d.datctype, "
            "(SELECT extversion FROM pg_extension WHERE extname = 'vector') "
            "FROM pg_database d WHERE d.datname = current_database()"
        )
    ).one()
    found_major, encoding, collate, ctype, vector = row
    expected = {
        "major version": (found_major, major),
        "encoding": (encoding, EXPECTED_ENCODING),
        "datcollate": (collate, EXPECTED_LOCALE),
        "datctype": (ctype, EXPECTED_LOCALE),
    }
    for name, (found, wanted) in expected.items():
        if found != wanted:
            raise ClusterRefusedError(
                f"The cluster's {name} is {found!r}, not {wanted!r}: it is not the "
                "cluster RFC-033a measured."
            )
    if vector is None:
        raise ClusterRefusedError(
            "The vector extension is not installed after the migrations."
        )
    if _version(vector) > _version(pgvector):
        raise ClusterRefusedError(
            f"vector {vector} is installed, newer than this runtime's {pgvector}."
        )
    if _version(vector) < _version(pgvector):
        return (
            f"vector {vector} is installed, older than this runtime's {pgvector}; "
            "ALTER EXTENSION vector UPDATE belongs to upgrades (RFC-033 section 14).",
        )
    return ()


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def prepare_database(
    connection: ClusterConnection,
    *,
    major: int,
    pgvector: str,
    on_migrating: Callable[[], None] | None = None,
) -> PreparedDatabase:
    """Section 7.4's three steps."""
    created = ensure_database(connection)
    engine = _engine(connection)
    try:
        with engine.connect() as db:
            heads, known = code_revisions()
            found = database_heads(db)
            comparison = compare_heads(found, heads, known)
            migrated_from: tuple[str, ...] | None = None
            if comparison is HeadComparison.BEHIND:
                # Double-checked under a lock: a second caller that arrives
                # while the first migrates waits here, then finds it current.
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                )
                found = database_heads(db)
                comparison = compare_heads(found, heads, known)
            if comparison is HeadComparison.UNKNOWN:
                db.rollback()
                raise ClusterRefusedError(
                    f"The database is at revision {', '.join(sorted(found))}, which "
                    "this code does not know: a newer SolidVision migrated it. "
                    "Running this code against a schema it does not know is worse "
                    "than not starting."
                )
            if comparison is HeadComparison.BEHIND:
                if on_migrating is not None:
                    on_migrating()
                migrated_from = tuple(sorted(found))
                upgrade(db)
            warnings = check_facts(db, major, pgvector)
            db.commit()
    finally:
        engine.dispose()
    return PreparedDatabase(
        created_database=created,
        migrated_from=migrated_from,
        heads=tuple(sorted(heads)),
        warnings=warnings,
    )
