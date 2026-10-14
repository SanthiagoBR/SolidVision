"""The engine, for each source of the connection (RFC-034 section 7).

No PostgreSQL server runs here. What reaches the driver is recorded where
SQLAlchemy hands it over, `psycopg.connect`, and one test lets the driver
dial a socket of the test's own -- the two things the pilot checked by hand
before the RFC was written. A real connection through this path belongs to
the `embedded` marker and to section 16.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.infrastructure.config.layout import Layout, layout_at
from app.infrastructure.config.settings import DatabaseSource, Settings, load_settings
from app.infrastructure.database.embedded.database_setup import (
    CONNECT_TIMEOUT_SECONDS,
    LIBPQ_OVERRIDES,
    ClusterConnection,
)
from app.infrastructure.database.embedded.errors import (
    ClusterNotRunningError,
    ClusterRefusedError,
    EmbeddedPostgresError,
    RuntimeInvalidError,
)
from app.infrastructure.persistence import engine as engine_module
from app.infrastructure.persistence.engine import EngineInstance, build_engine
from app.infrastructure.persistence.session import get_db
from app.presentation.api import app

PASSWORD = "p4ssw0rd-that-must-not-leak"
PORT = 51336


@pytest.fixture(autouse=True)
def libpq_variables_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever `PG*` variables this process had come back after each test:
    an embedded connection takes them out for good (section 7.5)."""
    for name in [name for name in os.environ if name.upper().startswith("PG")]:
        monkeypatch.setenv(name, os.environ[name])


def libpq_variables() -> list[str]:
    return sorted(name for name in os.environ if name.upper().startswith("PG"))


@pytest.fixture()
def dialled(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every set of parameters SQLAlchemy hands to the driver, which then
    dials nothing. `PG*` is which of libpq's variables were still in the
    environment at that instant."""
    calls: list[dict[str, Any]] = []

    def connect(*arguments: Any, **parameters: Any) -> None:
        calls.append({**parameters, "PG*": libpq_variables()})
        raise psycopg.OperationalError("recorded, not dialled")

    monkeypatch.setattr(psycopg, "connect", connect)
    return calls


@pytest.fixture()
def checkout(tmp_path: Path) -> Layout:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return layout_at(root, tmp_path / "profile")


def external(where: Layout, **overrides: Any) -> Settings:
    return load_settings(where, database_source="external", **overrides).settings


def embedded(where: Layout, **overrides: Any) -> Settings:
    """Constructor arguments, so that nothing in the environment -- which
    `run -- pytest` fills with the real cluster's directories -- decides."""
    return load_settings(
        where,
        database_source="embedded",
        embedded_postgres_directory=where.postgres_directory,
        embedded_postgres_runtime_directory=where.postgres_runtime,
        **overrides,
    ).settings


def found(port: int = PORT) -> ClusterConnection:
    return ClusterConnection(port=port, password=PASSWORD)


def attempt(engine: Any) -> OperationalError:
    with pytest.raises(OperationalError) as failure:
        engine.connect()
    return failure.value


# --------------------------------------------------------------------------
# external: the five DATABASE_* settings, as before
# --------------------------------------------------------------------------


def test_the_external_engine_is_built_from_the_five_settings(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    configuration = external(
        checkout,
        database_host="db.example",
        database_port=6543,
        database_name="photos",
        database_user="ana",
        database_password=PASSWORD,
    )

    engine = build_engine(configuration)
    attempt(engine)

    (parameters,) = dialled
    assert parameters["host"] == "db.example"
    assert parameters["port"] == 6543
    assert parameters["dbname"] == "photos"
    assert parameters["user"] == "ana"
    assert parameters["password"] == PASSWORD


def test_a_password_with_url_characters_arrives_whole(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    """`Settings.database_url` interpolated it into a string unescaped, and an
    `@` or a `/` broke the URL (RFC-033 section 7.2). `URL.create` escapes."""
    awkward = "p@ss:w/rd?#%"

    attempt(build_engine(external(checkout, database_password=awkward)))

    assert dialled[0]["password"] == awkward


def test_the_external_engine_gets_a_connect_timeout_and_nothing_else(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    """Without one, a refused port was still being tried after 25 s, which is
    how long `/health` took to say 503 (measured, section 2.3). The `PG*`
    variables stay the developer's with this source (section 7.5)."""
    attempt(build_engine(external(checkout)))

    assert dialled[0]["connect_timeout"] == CONNECT_TIMEOUT_SECONDS == 10
    assert not set(LIBPQ_OVERRIDES) & set(dialled[0])


# --------------------------------------------------------------------------
# embedded: the engine asks when it connects (section 7.3)
# --------------------------------------------------------------------------


def test_the_embedded_engine_is_built_without_a_port_and_without_a_password(
    checkout: Layout,
) -> None:
    engine = build_engine(embedded(checkout), locate=found)

    assert engine.url.render_as_string(hide_password=False) == (
        "postgresql+psycopg://solidvision@127.0.0.1/solidvision"
    )


def test_building_the_engine_asks_nothing_of_the_cluster(checkout: Layout) -> None:
    """Importing must not need the cluster up, nor even the runtime there:
    the API starts with the database stopped and answers 503 (RFC-008)."""
    asked: list[int] = []

    def locate() -> ClusterConnection:
        asked.append(1)
        return found()

    build_engine(embedded(checkout), locate=locate)
    build_engine(embedded(checkout))

    assert asked == []


def test_the_listener_supplies_the_port_and_the_password(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    attempt(build_engine(embedded(checkout), locate=found))

    (parameters,) = dialled
    assert parameters["host"] == "127.0.0.1"
    assert parameters["port"] == PORT
    assert parameters["password"] == PASSWORD
    assert parameters["user"] == "solidvision"
    assert parameters["dbname"] == "solidvision"


def test_the_cluster_is_asked_again_at_every_new_connection(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    """The port is chosen at every start of the server. A value read once, at
    import, would be wrong the day the cluster restarted on another port with
    the API still up (section 2.2)."""
    ports = iter([PORT, 60000])
    engine = build_engine(embedded(checkout), locate=lambda: found(next(ports)))

    attempt(engine)
    attempt(engine)

    assert [parameters["port"] for parameters in dialled] == [PORT, 60000]


def test_the_embedded_engine_passes_the_adapters_own_libpq_parameters(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    """From the same constant (section 7.5): libpq reads `PGOPTIONS`,
    `PGSSLMODE` and `PGGSSENCMODE` only for a parameter it was not given, so
    each is given, even when empty."""
    attempt(build_engine(embedded(checkout), locate=found))

    (parameters,) = dialled
    assert {name: parameters[name] for name in LIBPQ_OVERRIDES} == LIBPQ_OVERRIDES
    assert parameters["options"] == ""
    assert parameters["connect_timeout"] == CONNECT_TIMEOUT_SECONDS


def test_no_pg_variable_is_left_for_libpq_to_read_when_it_connects(
    checkout: Layout, dialled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variables no parameter answers: `PGTZ` and `PGDATESTYLE` are sent
    to the server as settings of the session, and `PGSERVICE` names a file
    entry (measured, section 16). They are taken out before libpq reads."""
    monkeypatch.setenv("PGTZ", "Mars/Phobos")
    monkeypatch.setenv("PGDATESTYLE", "banana")
    monkeypatch.setenv("PGSERVICE", "no-such-service")

    attempt(build_engine(embedded(checkout), locate=found))

    assert dialled[0]["PG*"] == []


def test_the_external_source_leaves_the_pg_variables_to_the_developer(
    checkout: Layout, dialled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGAPPNAME", "the developer's own")

    attempt(build_engine(external(checkout)))

    assert "PGAPPNAME" in dialled[0]["PG*"]
    assert os.environ["PGAPPNAME"] == "the developer's own"


def test_the_embedded_source_never_reads_the_database_settings(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    configuration = embedded(
        checkout,
        database_host="somebody-elses-postgres",
        database_port=5432,
        database_user="other",
        database_password="another-password",
        database_name="other",
    )

    attempt(build_engine(configuration, locate=found))

    (parameters,) = dialled
    assert "somebody-elses-postgres" not in parameters.values()
    assert "another-password" not in parameters.values()
    assert "other" not in parameters.values()
    assert parameters["port"] == PORT


def test_the_driver_dials_the_port_the_listener_supplied(checkout: Layout) -> None:
    """Not recorded this time: a socket of the test's own is listening, the
    listener is given its port, and the driver's first bytes arrive there --
    a startup packet of protocol 3, with no SSL request before it."""
    received: list[bytes] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        listening.settimeout(20)
        port = listening.getsockname()[1]

        def serve() -> None:
            accepted, _ = listening.accept()
            with accepted:
                accepted.settimeout(20)
                received.append(accepted.recv(8))

        server = threading.Thread(target=serve)
        server.start()
        engine = build_engine(
            embedded(checkout), locate=lambda: found(port), poolclass=NullPool
        )
        try:
            attempt(engine)
        finally:
            server.join(timeout=30)

    assert len(received) == 1 and len(received[0]) == 8
    assert received[0][4:6] == b"\x00\x03", "protocol 3, straight away"


# --------------------------------------------------------------------------
# A cluster that is not there (section 7.4): an error, never a fallback
# --------------------------------------------------------------------------


def refusing(error: EmbeddedPostgresError) -> Any:
    def locate() -> ClusterConnection:
        raise error

    return locate


@pytest.mark.parametrize(
    "error",
    [
        ClusterNotRunningError("The embedded cluster is not ready: stopped."),
        ClusterRefusedError("the password-cluster pair is lost."),
        RuntimeInvalidError("has no SOLIDVISION-RUNTIME.json"),
    ],
)
def test_a_cluster_that_cannot_be_located_is_a_connection_error_that_says_so(
    checkout: Layout, dialled: list[dict[str, Any]], error: EmbeddedPostgresError
) -> None:
    engine = build_engine(embedded(checkout), locate=refusing(error))

    failure = attempt(engine)

    assert isinstance(failure, SQLAlchemyError), "what /health already catches"
    assert str(error) in str(failure)
    assert dialled == [], "and nothing was dialled: no localhost:5432 instead"


def test_the_runtime_being_absent_is_found_out_at_the_connection_not_before(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    """The real locator, on a layout whose `runtime\\postgres` does not exist."""
    engine = build_engine(embedded(checkout))

    failure = attempt(engine)

    assert "SOLIDVISION-RUNTIME.json" in str(failure)
    assert dialled == []


def test_no_connection_error_carries_the_password(
    checkout: Layout, dialled: list[dict[str, Any]]
) -> None:
    failure = attempt(build_engine(embedded(checkout), locate=found))

    assert PASSWORD not in str(failure)
    assert PASSWORD not in repr(failure)


# --------------------------------------------------------------------------
# What the rest of the project sees
# --------------------------------------------------------------------------


def test_health_answers_503_and_not_500_when_the_cluster_is_absent(
    checkout: Layout,
) -> None:
    """Through the real route and a real session, bound to an embedded engine
    whose cluster is stopped."""
    engine = build_engine(
        embedded(checkout),
        locate=refusing(ClusterNotRunningError("The embedded cluster is stopped.")),
    )

    def session_on_a_stopped_cluster() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = session_on_a_stopped_cluster
    try:
        with TestClient(app) as client:
            response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy", "database": "disconnected"}


def test_a_query_through_the_engine_fails_the_same_way(checkout: Layout) -> None:
    engine = build_engine(
        embedded(checkout),
        locate=refusing(ClusterNotRunningError("The embedded cluster is stopped.")),
    )

    with pytest.raises(OperationalError, match="stopped"):
        with Session(engine) as session:
            session.execute(text("SELECT 1"))


def test_the_projects_engine_is_the_one_build_engine_builds() -> None:
    """One module-level singleton, as before; `session.py` did not change."""
    from app.infrastructure.config.settings import settings

    rebuilt = build_engine()

    assert EngineInstance.url == rebuilt.url
    assert EngineInstance.pool._pre_ping is True
    if settings.database_source is DatabaseSource.EMBEDDED:
        assert EngineInstance.url.port is None and EngineInstance.url.password is None
    else:
        assert EngineInstance.url.port == settings.database_port
    assert engine_module.DRIVER == "postgresql+psycopg"
