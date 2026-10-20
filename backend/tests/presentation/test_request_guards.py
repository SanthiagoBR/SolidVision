"""The `Host` and the `Origin`, in front of everything (RFC-037 section 5.2).

On an app of its own first, which has a route for each method, and then on the
real one, to hold that the middleware is installed there and stands before the
routing: an unknown path is refused like a known one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.presentation.api import app as real_app
from app.presentation.request_guards import (
    LoopbackRequestGuard,
    host_name,
    origin_is_loopback,
    refusal_for,
)

LOOPBACK = "http://127.0.0.1"


def an_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LoopbackRequestGuard)

    @app.get("/read")
    def read() -> dict[str, bool]:
        return {"read": True}

    @app.post("/write")
    def write() -> dict[str, bool]:
        return {"written": True}

    @app.delete("/write")
    def delete() -> dict[str, bool]:
        return {"deleted": True}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(an_app(), base_url=LOOPBACK) as started:
        yield started


class TestTheHost:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "127.0.0.1:51234", "localhost:51234", "LOCALHOST:8000"]
    )
    def test_the_loopback_names_pass(self, client: TestClient, host: str) -> None:
        response = client.get("/read", headers={"Host": host})

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example",
            "evil.example:51234",
            "testserver",
            "localhost.:8000",
            "127.0.0.1.evil.example",
            "[::1]:51234",
            "",
        ],
    )
    def test_any_other_name_is_a_400(self, client: TestClient, host: str) -> None:
        """DNS rebinding: the page's own name, now resolving to 127.0.0.1.
        `testserver` is the `TestClient`'s default, and is refused too."""
        response = client.get("/read", headers={"Host": host})

        assert response.status_code == 400
        assert response.json() == {"detail": "Invalid host header"}

    def test_the_host_is_checked_before_the_method_is(self, client: TestClient) -> None:
        response = client.post(
            "/write", headers={"Host": "evil.example", "Origin": LOOPBACK}
        )

        assert response.status_code == 400


class TestTheOrigin:
    @pytest.mark.parametrize("origin", ["http://evil.example", "null"])
    def test_a_write_from_another_origin_is_a_403(
        self, client: TestClient, origin: str
    ) -> None:
        """`null` is what a sandboxed frame or a `file:` page sends."""
        for response in (
            client.post("/write", headers={"Origin": origin}),
            client.delete("/write", headers={"Origin": origin}),
        ):
            assert response.status_code == 403
            assert "Cross-origin" in response.json()["detail"]

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:51234",
            "http://localhost:5173",
            "http://127.0.0.1",
        ],
    )
    def test_a_write_from_the_window_or_the_dev_proxy_passes(
        self, client: TestClient, origin: str
    ) -> None:
        response = client.post("/write", headers={"Origin": origin})

        assert response.status_code == 200
        assert response.json() == {"written": True}

    def test_a_write_without_an_origin_passes(self, client: TestClient) -> None:
        """`curl`, the scripts, the `TestClient` and the launcher's probe."""
        assert client.post("/write").status_code == 200

    def test_a_read_from_another_origin_passes(self, client: TestClient) -> None:
        """A read changes nothing, and the browser does not let the page read
        the answer of another origin: the `Host` already saw to the rest."""
        response = client.get("/read", headers={"Origin": "http://evil.example"})

        assert response.status_code == 200


class TestOnTheRealApp:
    """Without the `with`: no lifespan, so no warm-up, whatever the `.env` says.
    `/openapi.json` needs no database."""

    def test_the_testclients_default_host_is_refused(self) -> None:
        assert TestClient(real_app).get("/openapi.json").status_code == 400

    def test_loopback_reaches_it(self) -> None:
        client = TestClient(real_app, base_url=LOOPBACK)

        assert client.get("/openapi.json").status_code == 200

    def test_an_unknown_path_is_refused_like_a_known_one(self) -> None:
        client = TestClient(real_app, base_url=LOOPBACK)

        refused = client.post("/api/v1/nada", headers={"Origin": "http://evil.example"})
        unknown = client.post("/api/v1/nada")

        assert refused.status_code == 403
        assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("host", "name"),
    [("127.0.0.1:8000", "127.0.0.1"), ("LocalHost", "localhost"), (" x:1 ", "x")],
)
def test_the_name_of_a_host(host: str, name: str) -> None:
    assert host_name(host) == name


@pytest.mark.parametrize(
    ("origin", "loopback"),
    [
        ("http://127.0.0.1:51234", True),
        ("http://localhost", True),
        ("https://localhost", False),
        ("http://localhost.evil.example", False),
        ("http://[::1]:8000", False),
        ("http://[::1", False),
        ("null", False),
        ("", False),
    ],
)
def test_which_origins_are_loopback(origin: str, loopback: bool) -> None:
    assert origin_is_loopback(origin) is loopback


def test_a_missing_host_is_refused() -> None:
    refusal = refusal_for("GET", None, None)

    assert refusal is not None and refusal.status == 400


def test_options_is_a_read() -> None:
    assert refusal_for("OPTIONS", "127.0.0.1", "http://evil.example") is None
