"""The whole launcher, with the real API and job runner (RFC-036 section 10).

Marked `slow`: it loads CLIP from `runtime\\models` and starts a cluster from
`runtime\\postgres`, both in a temporary data root, and is skipped without
either. It is the fourth criterion of the roadmap, end to end: a job over the
45 images of RFC-022, created through the API, interrupted by the stop, and
resumed by the next start from its checkpoint, with the same `attempts`,
until every image of the scope is indexed.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.infrastructure.ai.model_runtime import ModelUnavailableError, default_runtime
from app.infrastructure.config.settings import settings
from app.infrastructure.database.embedded.errors import RuntimeInvalidError
from app.infrastructure.database.embedded.runtime import PostgresRuntime

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows processes"),
]

BACKEND = Path(__file__).resolve().parents[2]
DEMO_IMAGES = BACKEND / "dataset" / "demo" / "images"
DEMO_COUNT = 45
CREATE_NO_WINDOW = 0x08000000
START_SECONDS = 600.0
JOB_SECONDS = 900.0
STOP_SECONDS = 120.0


@dataclass
class Launcher:
    """One `python -m app.launcher --supervised`, and the events it wrote."""

    process: subprocess.Popen[str]
    events: list[dict[str, Any]] = field(default_factory=list)
    _reader: threading.Thread | None = None

    def __post_init__(self) -> None:
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.events.append(json.loads(line))

    def wait_for(self, name: str, seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for event in list(self.events):
                if event["event"] == name:
                    return event
                assert event["event"] != "failed", event
            assert self.process.poll() is None, self.events
            time.sleep(0.1)
        raise AssertionError(f"no {name} in {seconds:.0f} s: {self.events}")

    def stop(self) -> int:
        assert self.process.stdin is not None
        self.process.stdin.close()
        code = self.process.wait(STOP_SECONDS)
        if self._reader is not None:
            self._reader.join(10)
        return code


@pytest.fixture()
def environment(tmp_path: Path) -> dict[str, str]:
    try:
        PostgresRuntime(settings.embedded_postgres_runtime_directory).manifest()
        default_runtime().check_presence()
    except (RuntimeInvalidError, ModelUnavailableError) as error:
        pytest.skip(str(error))
    local = tmp_path / "Local"
    data = local / "SolidVision-dev"
    return {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "LOCALAPPDATA": str(local),
        "DATABASE_SOURCE": "embedded",
        "EMBEDDED_POSTGRES_DIRECTORY": str(data / "postgres"),
        "LOG_DIRECTORY": str(data / "logs"),
        "THUMBNAIL_DIRECTORY": str(data / "thumbnails"),
        "WARM_UP_MODELS": "false",
    }


@pytest.fixture()
def start(environment: dict[str, str], tmp_path: Path) -> Iterator[Any]:
    started: list[Launcher] = []

    def start_one() -> Launcher:
        stderr = (tmp_path / f"launcher-{len(started)}.txt").open("w")
        launcher = Launcher(
            subprocess.Popen(
                [sys.executable, "-m", "app.launcher", "--supervised"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                creationflags=CREATE_NO_WINDOW,
                text=True,
            )
        )
        started.append(launcher)
        return launcher

    yield start_one
    for launcher in started:
        if launcher.process.poll() is None:
            launcher.stop()


def request(url: str, method: str, path: str, body: object = None) -> Any:
    host, port = url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=60)
    try:
        payload = None if body is None else json.dumps(body)
        headers = {} if body is None else {"Content-Type": "application/json"}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        answer = json.loads(response.read() or b"null")
        assert response.status < 300, (response.status, answer)
        return answer
    finally:
        connection.close()


def queue_the_demo_corpus(url: str) -> str:
    """Register the volume the corpus is on, and queue a job over the corpus."""
    volumes = request(url, "GET", "/api/v1/volumes")["volumes"]
    images = str(DEMO_IMAGES).lower()
    volume = max(
        (
            v
            for v in volumes
            if images.startswith(v["mount_point"].lower().replace("/", "\\"))
        ),
        key=lambda v: len(v["mount_point"]),
    )
    device = request(
        url,
        "POST",
        "/api/v1/devices",
        {
            "volume_identity": volume["volume_identity"],
            "volume_kind": volume["volume_kind"],
            "label": "demo",
        },
    )
    scope = DEMO_IMAGES.relative_to(Path(volume["mount_point"]))
    job = request(
        url, "POST", "/api/v1/jobs", {"device_id": device["id"], "scopes": [str(scope)]}
    )
    return str(job["id"])


def job_of(url: str, job_id: str) -> dict[str, Any]:
    answer: dict[str, Any] = request(url, "GET", f"/api/v1/jobs/{job_id}")
    return answer


def test_a_long_job_stopped_in_the_middle_resumes_at_the_next_start(
    start: Any,
) -> None:
    first = start()
    url = first.wait_for("ready", START_SECONDS)["url"]
    job_id = queue_the_demo_corpus(url)

    deadline = time.monotonic() + JOB_SECONDS
    while (job := job_of(url, job_id))["processed_images"] < 8:
        assert job["status"] in ("pending", "running"), job
        assert time.monotonic() < deadline, job
        time.sleep(0.5)
    assert first.stop() == 0
    assert [event["event"] for event in first.events][-2:] == ["stopping", "stopped"]

    second = start()
    url = second.wait_for("ready", START_SECONDS)["url"]
    handed_back = job_of(url, job_id)
    assert handed_back["status"] in ("pending", "running")
    assert handed_back["attempts"] == 0
    assert handed_back["last_processed_relative_path"] is not None

    deadline = time.monotonic() + JOB_SECONDS
    while (job := job_of(url, job_id))["status"] != "completed":
        assert job["status"] in ("pending", "running"), job
        assert time.monotonic() < deadline, job
        time.sleep(0.5)
    devices = request(url, "GET", "/api/v1/devices")["devices"]

    assert job["attempts"] == 0
    assert sum(device["indexed_images"] for device in devices) == DEMO_COUNT
    assert second.stop() == 0
