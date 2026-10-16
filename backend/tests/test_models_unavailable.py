"""Where a model that cannot be loaded shows up, program by program (RFC-035 6.5).

    program                      what it does
    job_runner                   refuses to start: one line on stderr, exit 1
    API                          starts; search answers 503 with the line;
                                 /health keeps answering for the database
    CLI (search, index)          one line on stderr, exit 1

The three command-line programs run as real processes, because what is
promised is what a user -- or the launcher of RFC-036 -- sees: an exit code
and a line, and no traceback. They point `MODEL_RUNTIME_DIRECTORY` at an
empty directory, which a checkout reads from the environment, and their
`LOCALAPPDATA` at a temporary one, so nothing reaches the developer's data.
None of them gets as far as importing `torch`: the check of a start comes
before it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
import torch
from fastapi.testclient import TestClient

import app.presentation.dependencies as dependencies_module
from app.infrastructure.ai import model_runtime
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.ai.model_runtime import (
    CLIP,
    TRANSLATOR,
    ModelRuntime,
    read_lockfile,
)
from app.infrastructure.ai.query_translator import MarianQueryTranslator
from app.infrastructure.config.layout import Environment
from app.infrastructure.config.settings import settings
from app.infrastructure.persistence.session import get_db
from app.presentation.api import app
from app.presentation.dependencies import get_embedding_model, get_image_repository
from tests.application.fakes import FakeImageRepository

BACKEND = Path(__file__).resolve().parents[1]
CHILD_SECONDS = 300
REMEDY = "Materialize the models with `python scripts/vendor_models.py`."


@pytest.fixture()
def nowhere(tmp_path: Path) -> Path:
    directory = tmp_path / "no models here"
    directory.mkdir()
    return directory


def run(
    arguments: list[str], nowhere: Path, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    local = tmp_path / "Local"
    local.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "LOCALAPPDATA": str(local),
            "MODEL_RUNTIME_DIRECTORY": str(nowhere),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=CHILD_SECONDS,
    )


def last_line(completed: subprocess.CompletedProcess[str]) -> str:
    assert "Traceback" not in completed.stderr, completed.stderr[-3000:]
    return completed.stderr.strip().splitlines()[-1]


# --------------------------------------------------------------------------
# The command-line programs
# --------------------------------------------------------------------------


def test_the_job_runner_refuses_to_start_in_one_line(
    nowhere: Path, tmp_path: Path
) -> None:
    """Like a `ConfigurationError` (RFC-034 section 5.5): staying up would
    only claim jobs to fail each one, and restarting fixes nothing."""
    completed = run(
        ["-m", "app.infrastructure.workers.job_runner", "--once"], nowhere, tmp_path
    )

    assert completed.returncode == 1
    assert last_line(completed) == (
        f"SolidVision cannot start: The models are not in {nowhere}. {REMEDY}"
    )


def test_a_search_from_the_command_line_says_why_in_one_line(
    nowhere: Path, tmp_path: Path
) -> None:
    completed = run(
        ["-m", "app.cli", "search", "propriedade rural com um pequeno lago"],
        nowhere,
        tmp_path,
    )

    assert completed.returncode == 1
    assert last_line(completed) == f"The models are not in {nowhere}. {REMEDY}"
    assert completed.stdout == ""


def test_indexing_from_the_command_line_queues_nothing_and_says_why(
    nowhere: Path, tmp_path: Path
) -> None:
    """The check comes before the device is registered or a job is created:
    no job is left behind for a model that cannot run it."""
    completed = run(
        ["-m", "app.infrastructure.workers.indexing_worker", "--root", str(tmp_path)],
        nowhere,
        tmp_path,
    )

    assert completed.returncode == 1
    assert last_line(completed) == f"The models are not in {nowhere}. {REMEDY}"


# --------------------------------------------------------------------------
# The API
# --------------------------------------------------------------------------


class HealthySession:
    def execute(self, statement: object) -> None:
        return None


@pytest.fixture()
def broken_api(
    nowhere: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[TestClient]:
    """The API of an installation whose `runtime\\models` is not there, with
    warm-up on, as installed. The database is a stand-in: what is under test
    is that the models' absence does not reach it."""
    runtime = ModelRuntime(nowhere, read_lockfile(), Environment.DEVELOPMENT, None)
    device = torch.device("cpu")
    model = ClipEmbeddingModel(
        runtime.loader(CLIP),
        translator=MarianQueryTranslator(runtime.loader(TRANSLATOR), device=device),
        device=device,
    )

    def database() -> Generator[HealthySession, None, None]:
        yield HealthySession()

    monkeypatch.setattr(settings, "warm_up_models", True)
    monkeypatch.setattr(model_runtime, "default_runtime", lambda: runtime)
    monkeypatch.setattr(dependencies_module, "get_embedding_model", lambda: model)
    app.dependency_overrides[get_embedding_model] = lambda: model
    app.dependency_overrides[get_image_repository] = FakeImageRepository
    app.dependency_overrides[get_db] = database
    app_logger = logging.getLogger("app")
    app_logger.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app_logger.removeHandler(caplog.handler)
        app.dependency_overrides.clear()


def test_the_api_starts_and_logs_the_line_without_a_traceback(
    broken_api: TestClient, nowhere: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The warm-up ran when the fixture entered the client, in setup."""
    startup = caplog.get_records("setup")

    assert broken_api.get("/openapi.json").status_code == 200
    (failure,) = [record for record in startup if record.levelno >= logging.WARNING]
    assert failure.getMessage() == (
        f"Model warm-up failed, and search answers 503: The models are not in "
        f"{nowhere}. {REMEDY}"
    )
    assert failure.exc_info is None


def test_health_keeps_answering_for_the_database(broken_api: TestClient) -> None:
    response = broken_api.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_search_answers_503_with_the_line(
    broken_api: TestClient, nowhere: Path
) -> None:
    """503 and not 500: unavailable, not broken -- what `/health` answers
    for a database that is away. The `detail` is the line, whole."""
    response = broken_api.get("/api/v1/images/search", params={"q": "a lake"})

    assert response.status_code == 503
    assert response.json() == {"detail": f"The models are not in {nowhere}. {REMEDY}"}
