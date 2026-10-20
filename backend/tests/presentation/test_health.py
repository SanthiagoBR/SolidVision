"""`/health`: the database decides the status, and `search` says the rest.

RFC-008, and the two fields of RFC-036 section 5.2. With the warm-up off --
pinned here, whatever the developer's `.env` says -- `search` is `on_demand`:
the first search loads the models. The other three values are in
`test_api_in_two_times.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.config.settings import settings
from app.infrastructure.persistence.session import get_db
from app.presentation.api import app


class FakeSession:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def execute(self, statement: object) -> None:
        if self._error is not None:
            raise self._error


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    # A yield fixture is a generator function, so the annotation has to say
    # so -- `-> TestClient` described the value pytest hands the test, not
    # what the function returns. The `with` form matters and is why this
    # yields at all: it runs the app's lifespan, which is what the RFC-026
    # warm-up hangs off.
    monkeypatch.setattr(settings, "warm_up_models", False)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_health_returns_200_when_database_is_available(client: TestClient) -> None:
    def dependency() -> Generator[FakeSession, None, None]:
        yield FakeSession()

    app.dependency_overrides[get_db] = dependency
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "database": "connected",
        "version": "0.1.0",
        "environment": "development",
        "search": "on_demand",
        "search_detail": None,
    }


def test_health_returns_503_when_database_dependency_raises(client: TestClient) -> None:
    def failing_dependency() -> Generator[FakeSession, None, None]:
        yield FakeSession(error=SQLAlchemyError("database unavailable"))

    app.dependency_overrides[get_db] = failing_dependency
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "database": "disconnected",
        "search": "on_demand",
        "search_detail": None,
    }


def test_health_returns_expected_contract_for_healthy_response(
    client: TestClient,
) -> None:
    def dependency() -> Generator[FakeSession, None, None]:
        yield FakeSession()

    app.dependency_overrides[get_db] = dependency
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    payload = response.json()
    assert set(payload) == {
        "status",
        "database",
        "version",
        "environment",
        "search",
        "search_detail",
    }
    assert payload["status"] == "healthy"
    assert payload["database"] == "connected"


# --------------------------------------------------------------------------
# What it writes (RFC-037 section 5.3)
# --------------------------------------------------------------------------


@pytest.fixture
def app_records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Every record of the app's loggers, from DEBUG up: they do not propagate
    to the root, where `caplog` listens, so it is attached to them here."""
    logger = logging.getLogger("app")
    previous = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.setLevel(previous)
        logger.removeHandler(caplog.handler)


def test_a_200_writes_nothing(
    client: TestClient, app_records: pytest.LogCaptureFixture
) -> None:
    """The interface asks every 2 s while the model loads: two INFO lines a
    call were some 86 thousand lines a day in `api.log`."""

    def dependency() -> Generator[FakeSession, None, None]:
        yield FakeSession()

    app.dependency_overrides[get_db] = dependency
    app_records.clear()
    try:
        for _ in range(3):
            assert client.get("/health").status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert app_records.records == []


def test_a_503_still_says_so(
    client: TestClient, app_records: pytest.LogCaptureFixture
) -> None:
    def failing_dependency() -> Generator[FakeSession, None, None]:
        yield FakeSession(error=SQLAlchemyError("database unavailable"))

    app.dependency_overrides[get_db] = failing_dependency
    app_records.clear()
    try:
        assert client.get("/health").status_code == 503
    finally:
        app.dependency_overrides.clear()

    records = [
        (record.levelname, record.getMessage()) for record in app_records.records
    ]
    assert records == [("WARNING", "Database health check failed")]
