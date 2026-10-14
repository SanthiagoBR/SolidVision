"""Alembic environment configuration for the persistence infrastructure."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool

from app.infrastructure.logging.logger import get_logger
from app.infrastructure.persistence.base import Base
from app.infrastructure.persistence.engine import build_engine

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except KeyError:
        pass

logger = get_logger(__name__)

logger.info("Alembic environment started")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    logger.info("Alembic running in offline mode")
    # Offline, the URL only names the dialect: nothing connects. With the
    # embedded source it has no port and no password, by design (RFC-034 7.3).
    context.configure(
        url=build_engine(poolclass=pool.NullPool).url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_on(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    logger.info("Alembic running in online mode")
    # RFC-033 section 7.4: a caller that already holds a connection -- the
    # embedded cluster adapter -- hands it over in `config.attributes`, which
    # is Alembic's own pattern for sharing one. `alembic upgrade head` on the
    # command line hands over nothing and gets the project's own engine: the
    # one place that knows where the database is, for either source (RFC-034
    # section 7.6).
    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        _run_migrations_on(shared_connection)
        return

    connectable = build_engine(poolclass=pool.NullPool)

    with connectable.connect() as connection:
        _run_migrations_on(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
