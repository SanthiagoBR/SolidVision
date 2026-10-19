"""The job runner stopped by the end of its stdin, in the middle of a job (RFC-036).

A real process, because what is under test is the chain the launcher relies
on (section 7.1): the stdin closed, the watcher's SIGINT, the
`KeyboardInterrupt` landing in the main thread in the middle of a batch,
`release()`, and the executor ending instead of claiming the job again. The
repositories are the in-memory ones and the model is a fake that takes its
time, so nothing here needs a database or a checkpoint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows processes")

BACKEND = Path(__file__).resolve().parents[3]
CREATE_NO_WINDOW = 0x08000000
CHILD_SECONDS = 120
PHOTOS = 40
BATCH = 4

EXECUTOR = f"""
import datetime, json, sys, time
from pathlib import Path

from tests.application.fakes import (
    FakeDeviceLocator, FakeDeviceRepository, FakeImageRepository,
    StubContentHasher, make_device,
)
from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.domain.entities.indexing_job import IndexingJob
from app.domain.value_objects.job_id import JobId
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.infrastructure.process.supervision import stop_on_stdin_eof
from app.infrastructure.workers.job_runner import JobRunner


class SlowModel(FakeEmbeddingModel):
    def encode_images(self, images):
        time.sleep(0.4)  # where the main thread is when the stop comes
        return super().encode_images(images)


disk, report = Path(sys.argv[1]), Path(sys.argv[2])
device = make_device(label="HD2")
jobs = InMemoryIndexingJobRepository()
images = FakeImageRepository()
saved = []


def after_a_batch():
    saved.append(1)
    if len(saved) == 1:
        print("a batch is saved", flush=True)


images.on_save_indexed_many = after_a_batch
runner = JobRunner(
    jobs=jobs,
    devices=FakeDeviceRepository([device]),
    locator=FakeDeviceLocator({{device.id: disk}}),
    indexer=IndexOrUpdateImagesUseCase(
        repository=images,
        embedding_model=SlowModel(),
        content_hasher=StubContentHasher(),
        batch_size={BATCH},
        metadata_prefetch_size=8,
    ),
    supported_extensions=(".jpg",),
    extract_capture_date=False,
    poll_interval=0.1,
)
job = jobs.create(
    IndexingJob(
        id=JobId.new(),
        device_id=device.id,
        scopes=(),
        created_at=datetime.datetime.now(tz=datetime.UTC),
    )
)
stop_on_stdin_eof()
runner.run_forever()
stored = jobs.get(job.id)
report.write_text(json.dumps({{
    "status": stored.status.value,
    "attempts": stored.attempts,
    "checkpoint": str(stored.last_processed_relative_path),
    "batches": len(saved),
}}))
"""


def test_closing_stdin_mid_job_hands_the_job_back_and_ends_the_executor(
    tmp_path: Path,
) -> None:
    disk = tmp_path / "disk"
    disk.mkdir()
    for index in range(PHOTOS):
        (disk / f"photo_{index:03d}.jpg").write_bytes(b"data")
    report = tmp_path / "report.json"
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", EXECUTOR, str(disk), str(report)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "LOCALAPPDATA": str(tmp_path / "Local"),
        },
        creationflags=CREATE_NO_WINDOW,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    try:
        assert process.stdout.readline().strip() == "a batch is saved"
        time.sleep(0.2)

        closed_at = time.perf_counter()
        process.stdin.close()
        code = process.wait(CHILD_SECONDS)
        elapsed = time.perf_counter() - closed_at
    finally:
        if process.poll() is None:
            process.kill()

    finished = json.loads(report.read_text())
    assert code == 0
    assert elapsed < 5.0
    assert finished["status"] == "pending"
    assert finished["attempts"] == 0
    assert finished["checkpoint"] != "None"
    assert finished["batches"] < PHOTOS // BATCH
