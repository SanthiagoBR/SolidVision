"""SQLAlchemy engine configuration for the persistence infrastructure.

Two sources for the connection (RFC-034 section 7.1), and one function that
builds the engine for either:

`external` -- the five `DATABASE_*` settings: the `.env` and the Docker
container. The URL is whole when the engine is built.

`embedded` -- the user's cluster. Its port is chosen at every start of the
server and can change under a running API (RFC-033 section 8.2), so the
engine is built **without a port and without a password**, and a `do_connect`
listener asks the adapter for both at every new physical connection. That is
what lets this module be imported with the cluster stopped, lets the port
move, and keeps the password out of `Settings` and out of every process's
environment. A cluster that is not there is a connection error that says so,
and never an attempt on `localhost:5432` (section 7.4). The environment
reaches no such connection: the libpq parameters the adapter gives its own,
and no `PG*` variable left in the process (section 7.5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import Pool

from app.infrastructure.config.settings import DatabaseSource, Settings, settings
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.database_setup import (
    CONNECT_TIMEOUT_SECONDS,
    DATABASE,
    LIBPQ_OVERRIDES,
    LOOPBACK,
    SUPERUSER,
    ClusterConnection,
    discard_libpq_environment,
)
from app.infrastructure.database.embedded.errors import EmbeddedPostgresError
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

DRIVER = "postgresql+psycopg"

Locate = Callable[[], ClusterConnection]


def build_engine(
    configuration: Settings = settings,
    *,
    poolclass: type[Pool] | None = None,
    locate: Locate | None = None,
) -> Engine:
    """The engine for `configuration`'s database source.

    `connect_timeout` goes to both: without it, a connection to a port nobody
    listens on was still being attempted after 25 s, and that is how long
    `/health` took to answer 503 (RFC-034 section 2.3).

    `locate` replaces the adapter's own `locate()`; it is for tests.
    """
    options: dict[str, Any] = {
        "echo": configuration.debug,
        "future": True,
        "pool_pre_ping": True,
    }
    if poolclass is not None:
        options["poolclass"] = poolclass
    if configuration.database_source is DatabaseSource.EMBEDDED:
        return _embedded_engine(locate or _cluster_locator(configuration), options)
    return create_engine(
        # `URL.create` escapes the password; interpolating it into a string,
        # as `Settings.database_url` used to, did not (RFC-033 section 7.2).
        URL.create(
            DRIVER,
            username=configuration.database_user,
            password=configuration.database_password,
            host=configuration.database_host,
            port=configuration.database_port,
            database=configuration.database_name,
        ),
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        **options,
    )


def _embedded_engine(locate: Locate, options: dict[str, Any]) -> Engine:
    engine = create_engine(
        URL.create(DRIVER, username=SUPERUSER, host=LOOPBACK, database=DATABASE),
        # The parameters the adapter gives its own connections, from the same
        # constant: libpq reads `PGOPTIONS`, `PGSSLMODE` and `PGGSSENCMODE`
        # only for a parameter it was not given (section 7.5).
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS, **LIBPQ_OVERRIDES},
        **options,
    )

    @event.listens_for(engine, "do_connect")
    def supply_the_cluster(
        dialect: Any, connection_record: Any, cargs: Any, cparams: dict[str, Any]
    ) -> None:
        try:
            found = locate()
        except EmbeddedPostgresError as error:
            # The exception SQLAlchemy wraps into its own `OperationalError`,
            # which `/health` already answers with 503 (RFC-008).
            raise psycopg.OperationalError(str(error)) from None
        # The variables no parameter answers (`PGTZ`, `PGSERVICE`...), taken
        # out of the process right before libpq reads them (RFC-034 7.5).
        discard_libpq_environment()
        cparams.update(
            host=found.host,
            port=found.port,
            user=found.user,
            password=found.password,
            dbname=found.database,
        )

    return engine


def _cluster_locator(configuration: Settings) -> Locate:
    """`locate()` on the user's cluster, built on the first connection.

    Not when the engine is built: importing this module must not need the
    runtime to be there, let alone the cluster to be up (section 7.3).
    """
    clusters: list[EmbeddedPostgres] = []

    def locate() -> ClusterConnection:
        if not clusters:
            clusters.append(
                EmbeddedPostgres(
                    runtime=PostgresRuntime(
                        configuration.embedded_postgres_runtime_directory
                    ),
                    home=ClusterHome(configuration.embedded_postgres_directory),
                )
            )
        return clusters[0].locate()

    return locate


EngineInstance: Engine = build_engine()

logger.info("SQLAlchemy engine created")
