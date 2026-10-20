"""The API in two times: the port first, the models after (RFC-036 section 5.3).

The warm-up runs in a thread, and `/health` says how it is going in `search`
(section 5.2); that importing the API imports no `torch` is held by
`tests/test_launcher_imports_architecture.py`. The program that the
launcher runs, `python -m app.presentation.api`, is exercised as a process at
the end: it opens its port, stops at the end of its stdin, and says 3 when the
port was taken.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import IO

import pytest
from fastapi.testclient import TestClient

import app.presentation.api as api_module
import app.presentation.dependencies as dependencies_module
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.ai import model_runtime
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.ai.model_runtime import ModelUnavailableError
from app.infrastructure.config.settings import settings
from app.infrastructure.persistence.session import get_db
from app.presentation.api import app
from app.presentation.model_warm_up import (
    ModelWarmUp,
    SearchReadiness,
    SearchState,
    model_warm_up,
)

BACKEND = Path(__file__).resolve().parents[2]
CREATE_NO_WINDOW = 0x08000000
PROCESS_SECONDS = 120
WARM_UP_SECONDS = 30


class HealthySession:
    def execute(self, statement: object) -> None:
        return None


@pytest.fixture()
def database() -> Iterator[None]:
    def healthy() -> Generator[HealthySession, None, None]:
        yield HealthySession()

    app.dependency_overrides[get_db] = healthy
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def search_of(client: TestClient) -> dict[str, object]:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    return {"search": payload["search"], "search_detail": payload["search_detail"]}


# --------------------------------------------------------------------------
# What /health says of search
# --------------------------------------------------------------------------


def test_with_the_warm_up_off_search_is_on_demand(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "warm_up_models", False)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert search_of(client) == {"search": "on_demand", "search_detail": None}


def test_while_the_warm_up_runs_search_is_loading_and_health_answers(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The port is open and `/health` is 200 while the models load: what does
    not need them is not kept waiting for them."""
    release = threading.Event()

    def slow_warm_up() -> SearchReadiness:
        release.wait(WARM_UP_SECONDS)
        return SearchReadiness(SearchState.READY)

    monkeypatch.setattr(settings, "warm_up_models", True)
    monkeypatch.setattr(api_module, "_warm_up_models", slow_warm_up)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            assert search_of(client) == {"search": "loading", "search_detail": None}
        finally:
            release.set()
        assert model_warm_up.wait(WARM_UP_SECONDS)
        assert search_of(client) == {"search": "ready", "search_detail": None}


def test_a_warm_up_that_could_not_load_says_why(
    database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = "The models are not in C:\\nowhere. Reinstall SolidVision."

    def refused() -> SearchReadiness:
        return SearchReadiness(SearchState.UNAVAILABLE, line)

    monkeypatch.setattr(settings, "warm_up_models", True)
    monkeypatch.setattr(api_module, "_warm_up_models", refused)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert model_warm_up.wait(WARM_UP_SECONDS)
        assert search_of(client) == {"search": "unavailable", "search_detail": line}


# --------------------------------------------------------------------------
# What the warm-up comes to
# --------------------------------------------------------------------------


class Present:
    def check_presence(self) -> None:
        return None


class Recording(EmbeddingModelPort):
    def __init__(self, error: Exception | None = None) -> None:
        self.encoded: list[str] = []
        self._error = error
        self._delegate = FakeEmbeddingModel()

    def encode_text(self, text: str) -> EmbeddingVector:
        if self._error is not None:
            raise self._error
        self.encoded.append(text)
        return self._delegate.encode_text(text)

    def encode_image(self, image: Image) -> EmbeddingVector:
        return self._delegate.encode_image(image)


@pytest.fixture()
def present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_runtime, "default_runtime", Present)


def test_a_warm_up_that_loaded_both_models_is_ready(
    present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = Recording()
    monkeypatch.setattr(dependencies_module, "get_embedding_model", lambda: model)

    assert api_module._warm_up_models() == SearchReadiness(SearchState.READY)
    assert model.encoded == [api_module._WARM_UP_QUERY]


def test_a_model_that_cannot_be_loaded_is_unavailable_with_its_line(
    present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = Recording(ModelUnavailableError("the line of RFC-035 section 6.4"))
    monkeypatch.setattr(dependencies_module, "get_embedding_model", lambda: model)

    assert api_module._warm_up_models() == SearchReadiness(
        SearchState.UNAVAILABLE, "the line of RFC-035 section 6.4"
    )


def test_any_other_failure_leaves_search_to_the_first_query(
    present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = Recording(RuntimeError("something transient"))
    monkeypatch.setattr(dependencies_module, "get_embedding_model", lambda: model)

    assert api_module._warm_up_models() == SearchReadiness(SearchState.ON_DEMAND)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_warm_up_that_raises_anyway_does_not_stay_loading() -> None:
    """`_warm_up_models` catches everything; if a defect let something through,
    `/health` must not say `loading` for ever. The error is the thread's."""

    def broken() -> SearchReadiness:
        raise RuntimeError("not supposed to happen")

    warm_up = ModelWarmUp()
    warm_up.start(broken)

    assert warm_up.wait(WARM_UP_SECONDS)
    assert warm_up.readiness() == SearchReadiness(SearchState.ON_DEMAND)


# --------------------------------------------------------------------------
# The program the launcher runs
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_api(
    port: int, tmp_path: Path, output: IO[bytes] | None = None
) -> subprocess.Popen[bytes]:
    """The program as the launcher starts it. `output`, when given, receives
    both of its streams, as the launcher's readers would."""
    return subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "app.presentation.api",
            "--port",
            str(port),
            "--supervised",
        ],
        stdin=subprocess.PIPE,
        stdout=output if output is not None else subprocess.DEVNULL,
        stderr=output if output is not None else subprocess.DEVNULL,
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "LOCALAPPDATA": str(tmp_path / "Local"),
            "WARM_UP_MODELS": "false",
        },
        creationflags=CREATE_NO_WINDOW,
    )


def _answer(port: int) -> int | None:
    """`/openapi.json`, which needs no database: what is asserted here is the
    port and the stop, and `/health` waits for whatever database the checkout
    points at -- up to the connect timeout, with Docker stopped."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/openapi.json")
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows processes")
def test_the_api_program_serves_its_port_and_stops_at_the_end_of_stdin(
    tmp_path: Path,
) -> None:
    """And writes no line per request (RFC-037 section 5.3): the launcher
    passes what the API writes to its stderr, and the window to its log."""
    port = _free_port()
    output_path = tmp_path / "api-output.txt"
    output = output_path.open("wb")
    process = _start_api(port, tmp_path, output)
    try:
        deadline = time.monotonic() + PROCESS_SECONDS
        while (status := _answer(port)) is None:
            assert process.poll() is None, "the API exited before it answered"
            assert time.monotonic() < deadline, "the API never answered"
            time.sleep(0.1)
        assert status == 200

        assert process.stdin is not None
        process.stdin.close()
        code = process.wait(PROCESS_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
        output.close()

    assert code == 0
    assert _answer(port) is None
    written = output_path.read_text(encoding="utf-8", errors="replace")
    assert "Uvicorn running on" in written, "the output was captured"
    assert '"GET /openapi.json' not in written


@pytest.mark.skipif(sys.platform != "win32", reason="Windows processes")
def test_the_api_program_says_3_when_its_port_is_taken(tmp_path: Path) -> None:
    """uvicorn's `STARTUP_FAILURE`: the launcher's cue to try another port
    (section 5.4)."""
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = int(holder.getsockname()[1])

        process = _start_api(port, tmp_path)
        try:
            code = process.wait(PROCESS_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()

    assert code == 3


def test_the_program_builds_its_server_without_an_access_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC-037 section 5.3. `python -m uvicorn app.presentation.api:app`, the
    run by hand, keeps uvicorn's default and its access log."""
    import uvicorn

    import app.presentation.api.__main__ as program

    configs: list[dict[str, object]] = []

    class Server:
        def __init__(self, config: dict[str, object]) -> None:
            configs.append(config)

        def run(self) -> None:
            return None

    def config(*arguments: object, **options: object) -> dict[str, object]:
        return options

    monkeypatch.setattr(uvicorn, "Config", config)
    monkeypatch.setattr(uvicorn, "Server", Server)

    assert program.main(["--port", "51234"]) == 0
    assert [options["access_log"] for options in configs] == [False]
