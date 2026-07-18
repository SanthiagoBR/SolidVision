from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.persistence.session import get_db
from app.presentation.api import app


class FakeSession:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def execute(self, statement: object) -> None:
        if self._error is not None:
            raise self._error


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
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
    assert set(payload) == {"status", "database", "version", "environment"}
    assert payload["status"] == "healthy"
    assert payload["database"] == "connected"
