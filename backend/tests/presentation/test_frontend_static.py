"""The interface served by the API, and the 404 it leaves alone (RFC-037 5.1).

An app of its own, shaped like the real one -- a router under `/api/v1`, and
`/health` outside it -- with a build of its own in a temporary directory: the
suite needs no `npm run build`, and whether the checkout has one changes
nothing here.

The property the design exists for is in `TestTheApisNotFoundIsUnchanged`:
`POST` to an unknown path is still a 404, and not the 405 a mount or a route at
`/` would give it (section 2.2).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.presentation.frontend import is_interface_path, serve_frontend

INDEX = "<!doctype html><title>SolidVision</title><div id=root></div>\n"
SCRIPT = "console.log('the build');\n"
NOT_FOUND = {"detail": "Not Found"}


def an_app() -> FastAPI:
    app = FastAPI()
    api = APIRouter(prefix="/api/v1")

    @api.get("/things")
    def things() -> dict[str, list[str]]:
        return {"things": []}

    @api.get("/hidden")
    def hidden() -> None:
        """A route that answers as if it did not exist: the reveal switched off."""
        raise HTTPException(status_code=404)

    @api.post("/things/{thing_id}/act")
    def act(thing_id: str) -> dict[str, str]:
        return {"acted": thing_id}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    app.include_router(api)
    return app


def write(path: Path, text: str) -> None:
    """Byte for byte: on Windows, `write_text` would end each line with CRLF."""
    path.write_bytes(text.encode("utf-8"))


def a_build(directory: Path) -> Path:
    (directory / "assets").mkdir(parents=True)
    write(directory / "index.html", INDEX)
    write(directory / "assets" / "index-3f2a.js", SCRIPT)
    return directory


def a_client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = an_app()
    assert serve_frontend(app, a_build(tmp_path / "dist"))
    with a_client(app) as started:
        yield started


class TestTheInterfaceIsServed:
    def test_the_root_is_the_index(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert response.text == INDEX
        assert response.headers["content-type"].startswith("text/html")

    @pytest.mark.parametrize("screen", ["/qualquer/tela", "/busca", "/pastas/HD2/"])
    def test_any_screen_is_the_index(self, client: TestClient, screen: str) -> None:
        response = client.get(screen)

        assert response.status_code == 200
        assert response.text == INDEX

    def test_the_index_is_revalidated_every_time(self, client: TestClient) -> None:
        """A new build is a new `index.html` naming new files: no stale copy."""
        assert client.get("/").headers["cache-control"] == "no-cache"

    def test_a_head_of_a_screen_is_answered_without_a_body(
        self, client: TestClient
    ) -> None:
        response = client.head("/qualquer/tela")

        assert response.status_code == 200
        assert response.content == b""

    def test_a_file_of_the_build_is_served_with_its_etag(
        self, client: TestClient
    ) -> None:
        response = client.get("/assets/index-3f2a.js")

        assert response.status_code == 200
        assert response.text == SCRIPT
        assert "etag" in response.headers

    def test_a_file_the_build_does_not_have_is_a_404(self, client: TestClient) -> None:
        """Not the index: an old `index.html` naming another build's files
        fails visibly, instead of receiving HTML for JavaScript."""
        response = client.get("/assets/falta.js")

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_the_routes_still_answer(self, client: TestClient) -> None:
        assert client.get("/api/v1/things").json() == {"things": []}
        assert client.get("/health").json() == {"status": "healthy"}
        assert client.post("/api/v1/things/7/act").json() == {"acted": "7"}


class TestTheApisNotFoundIsUnchanged:
    def test_an_unknown_api_path_is_the_json_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/nada")

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_a_post_to_an_unknown_path_is_a_404_and_not_a_405(
        self, client: TestClient
    ) -> None:
        """Section 2.2: the reveal switched off answers 404 to look like this."""
        for path in ("/api/v1/nada", "/api/v1/images/x/reveal", "/qualquer/tela"):
            response = client.post(path)
            assert response.status_code == 404, path
            assert response.json() == NOT_FOUND, path

    def test_a_route_that_answers_404_keeps_its_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/hidden")

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_the_bare_prefixes_are_not_screens(self, client: TestClient) -> None:
        assert client.get("/api").status_code == 404
        assert client.get("/assets").status_code == 404


class TestWithoutABuild:
    def test_without_an_index_nothing_is_served(self, tmp_path: Path) -> None:
        app = an_app()

        assert not serve_frontend(app, tmp_path / "dist")
        with a_client(app) as client:
            response = client.get("/")

        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    def test_without_the_assets_their_files_are_404s_and_not_500s(
        self, tmp_path: Path
    ) -> None:
        build = tmp_path / "dist"
        build.mkdir()
        write(build / "index.html", INDEX)
        app = an_app()

        assert serve_frontend(app, build)
        with a_client(app) as client:
            assert client.get("/").text == INDEX
            assert client.get("/assets/index-3f2a.js").status_code == 404


@pytest.mark.parametrize(
    ("path", "interface"),
    [
        ("/", True),
        ("/busca", True),
        ("/health/x", True),
        ("/apis", True),
        ("/assetsx/y", True),
        ("/api", False),
        ("/api/v1/images/search", False),
        ("/assets", False),
        ("/assets/index-3f2a.js", False),
    ],
)
def test_which_paths_are_screens(path: str, interface: bool) -> None:
    assert is_interface_path(path) is interface
