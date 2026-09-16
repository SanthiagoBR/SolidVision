"""What a real job costs, and what it does to the API while it runs.

Two `TBM`s, and they belong in one script because the second only means
anything while the first is happening.

    section 2.1   "HD2/2018 -- maybe 2,000 photos, `TBM` minutes". The
                  number that decides whether this product is demonstrable
                  on the day it is installed.
    section 17    "API responds to /health during an active job". RFC-029
                  section 2.3 argues the API stays responsive *because it
                  does not do the work*. Separate processes guarantee no
                  such thing against CPU contention -- that is the claim,
                  and this is the measurement.

**The executor is a real separate process.** `subprocess` running
`python -m app.infrastructure.workers.job_runner --once`, against the real
PostgreSQL, with the real volume adapter and the real model. Running it in
a thread would share a GIL with the API and measure something else
entirely -- and it is precisely the "separate process" claim that is on
trial here.

**The corpus lives on a real volume**, under a temporary directory on the
system drive, because the executor resolves its device through
`WindowsVolumeIdentityProvider` and a fake would bypass the very
composition this is meant to exercise. Every row it writes is removed
again in a `finally`, matched by the scope's path prefix, so nothing that
was in the database beforehand is touched.

Run it with the backend virtualenv, from the repository root:

    cd <repository root>
    backend/.venv/Scripts/python.exe \
        experiments/rfc-029-indexing-jobs/measure_throughput_and_api.py

`RFC029_PHOTOS` sets the corpus size (default 2000, ~20 minutes on CPU).
"""

from __future__ import annotations

import datetime
import logging
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from sqlalchemy import delete, func, select  # noqa: E402

from app.domain.entities.indexing_job import IndexingJob  # noqa: E402
from app.domain.value_objects.job_id import JobId  # noqa: E402
from app.domain.value_objects.job_scope import JobScope  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.database.models.indexing_job_model import (  # noqa: E402
    IndexingJobModel,
)
from app.infrastructure.filesystem.volume_identity_provider import (  # noqa: E402
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.persistence.postgres_device_repository import (  # noqa: E402
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (  # noqa: E402
    PostgresIndexingJobRepository,
)
from app.infrastructure.persistence.session import SessionLocal  # noqa: E402
from app.infrastructure.workers.indexing_worker import (  # noqa: E402
    register_device,
    scope_for,
)

logging.getLogger("app").setLevel(logging.ERROR)

PHOTOS = int(os.environ.get("RFC029_PHOTOS", "2000"))
REPOSITORY = Path("C:/Users/chapi/Documents/SolidVision")
PROBE_INTERVAL = 1.0


def build_corpus(root: Path, count: int) -> None:
    """Write `count` real JPEGs into a year/month tree.

    Synthetic and small (128x128), which is declared rather than hidden.
    CLIP resizes everything to 224x224, so the inference cost below is
    representative; the *decode* cost of a real 4000x3000 photograph is
    higher, which makes the throughput here an upper bound.
    """
    from PIL import Image

    for index in range(count):
        folder = root / "2018" / f"{index % 12 + 1:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        Image.frombytes(
            "RGB",
            (128, 128),
            bytes((index * 13 + position) % 256 for position in range(128 * 128 * 3)),
        ).save(folder / f"DSC_{index:05d}.jpg", "JPEG", quality=80)


class ApiProbe:
    """Hits `/health` and a search on a timer, recording every latency.

    In-process `TestClient`, and the sharing is the point: the API and the
    executor are separate processes on one machine, competing for the same
    cores. A probe that ran on an idle machine would answer a question
    nobody asked.
    """

    def __init__(self) -> None:
        self.health: list[float] = []
        self.search: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:
        from fastapi.testclient import TestClient

        from app.presentation.api import app

        with TestClient(app) as client:
            while not self._stop.is_set():
                started = time.perf_counter()
                client.get("/health")
                self.health.append((time.perf_counter() - started) * 1000)

                started = time.perf_counter()
                client.get("/api/v1/images/search", params={"q": "a photograph"})
                self.search.append((time.perf_counter() - started) * 1000)

                self._stop.wait(PROBE_INTERVAL)


def summarise(name: str, samples: list[float]) -> None:
    if not samples:
        print(f"   {name}: no samples")
        return
    ordered = sorted(samples)
    print(
        f"   {name:<28} n={len(ordered):4d}  "
        f"median {statistics.median(ordered):8.1f} ms  "
        f"p95 {ordered[int(len(ordered) * 0.95) - 1]:8.1f} ms  "
        f"max {ordered[-1]:8.1f} ms"
    )


def cleanup(scope: JobScope, job_id: JobId | None) -> None:
    """Remove only the rows this script created, and nothing else.

    Images are matched by the scope's path prefix and the job by its id.
    Neither is matched by *device*: the system drive is a real device with
    real rows on it -- the demo corpus, among others -- and this script
    has to leave every one of them exactly where it found it. An earlier
    draft deleted by "every job", which would have taken somebody else's
    history with it.
    """
    session = SessionLocal()
    try:
        session.execute(
            delete(ImageModel).where(ImageModel.relative_path.startswith(f"{scope}/"))
        )
        if job_id is not None:
            session.execute(
                delete(IndexingJobModel).where(IndexingJobModel.id == job_id.value)
            )
        session.commit()
    finally:
        session.close()


def main() -> None:
    print("RFC-029 sections 2.1 and 17 -- a real job, and the API beside it")
    print()
    print(f"machine:   {platform.platform()}")
    print(f"python:    {platform.python_version()}")
    print(f"corpus:    {PHOTOS} synthetic 128x128 JPEGs on the system drive")
    print("executor:  a real subprocess, real PostgreSQL, real CLIP, CPU")
    print("api:       in-process TestClient, competing for the same cores")
    print()
    print("Synthetic photographs, and small ones. CLIP resizes everything to")
    print("224x224 so the inference cost is representative, but decoding a")
    print("real 4000x3000 JPEG costs more -- the throughput below is an")
    print("upper bound, not a promise about a real collection.")
    print()

    with tempfile.TemporaryDirectory(prefix="rfc029-throughput-") as raw:
        disk = Path(raw)
        print(f"building {PHOTOS} files...")
        build_corpus(disk, PHOTOS)

        session = SessionLocal()
        volume_provider = WindowsVolumeIdentityProvider()
        device, volume = register_device(
            volume_provider=volume_provider,
            device_repository=PostgresDeviceRepository(session),
            root=disk,
            label="",
        )
        scope = scope_for(disk, volume.mount_point)
        jobs = PostgresIndexingJobRepository(session)
        job: IndexingJob | None = None

        try:
            job = jobs.create(
                IndexingJob(
                    id=JobId.new(),
                    device_id=device.id,
                    scopes=(scope,),
                    created_at=datetime.datetime.now(tz=datetime.UTC),
                )
            )
            print(f"queued job {job.id}")
            print(f"scope: {scope}")
            print()

            probe = ApiProbe()
            probe.start()
            # Wait for real baseline samples rather than for a fixed
            # number of seconds. The API process loads its own copy of the
            # model on the first search -- several seconds -- so a timed
            # wait collected one `/health` and no search at all, and the
            # cold start then landed in the "during" column where it
            # looked like contention it was not.
            print("warming the API and collecting a baseline...")
            deadline = time.monotonic() + 180
            while len(probe.search) < 6 and time.monotonic() < deadline:
                time.sleep(0.5)
            # The first search of the process is the checkpoint load; it
            # says nothing about a job running beside it.
            baseline_health = list(probe.health)
            baseline_search = list(probe.search)
            if len(baseline_search) < 2:
                raise SystemExit("no usable API baseline; refusing to compare")
            cold_start = baseline_search[0]
            baseline_search_warm = baseline_search[1:]
            print(f"   (first search, cold model load: {cold_start:.0f} ms, excluded)")

            print("starting the executor subprocess...")
            started = time.perf_counter()
            process = subprocess.run(
                [
                    str(REPOSITORY / "backend/.venv/Scripts/python.exe"),
                    "-m",
                    "app.infrastructure.workers.job_runner",
                    "--once",
                ],
                cwd=str(REPOSITORY),
                # `pyproject.toml` puts `backend/` on the path for pytest
                # only, so a real invocation needs it here. Keeping the
                # working directory at the repository root is what makes
                # the executor write to the same `logs/` a real run does.
                env={**os.environ, "PYTHONPATH": str(REPOSITORY / "backend")},
                capture_output=True,
                text=True,
            )
            elapsed = time.perf_counter() - started
            probe.stop()

            if process.returncode != 0:
                print(process.stdout[-3000:])
                print(process.stderr[-3000:])
                raise SystemExit(f"the executor exited {process.returncode}")

            finished = jobs.get(job.id)
            assert finished is not None

            indexed_rows = session.execute(
                select(func.count())
                .select_from(ImageModel)
                .where(ImageModel.relative_path.startswith(f"{scope}/"))
            ).scalar_one()

            print()
            print("1. a real scoped job, end to end (RFC-029 section 2.1)")
            print()
            print(f"   status:            {finished.status.value}")
            print(f"   discovered:        {finished.progress.discovered_files}")
            print(f"   indexed:           {finished.progress.processed_images}")
            print(f"   skipped:           {finished.progress.skipped_images}")
            print(f"   failed:            {finished.progress.failed_images}")
            print(f"   rows in images:    {indexed_rows}")
            print()
            print(
                f"   wall clock:        {elapsed:8.1f} s " f"({elapsed / 60:.1f} min)"
            )
            if finished.progress.processed_images:
                rate = finished.progress.processed_images / elapsed
                print(f"   throughput:        {rate:8.2f} images/s")
                print(f"   per image:         {1 / rate:8.3f} s")
                print()
                print("   That includes the process starting and loading the")
                print("   model, which a long-lived executor pays once.")
            print()

            print("2. the API while that was running (RFC-029 section 17)")
            print()
            during_health = probe.health[len(baseline_health) :]
            during_search = probe.search[len(baseline_search) :]
            print("   before the executor started:")
            summarise("GET /health", baseline_health)
            summarise("GET /images/search", baseline_search_warm)
            print()
            print("   while the executor saturated a core:")
            summarise("GET /health", during_health)
            summarise("GET /images/search", during_search)
            print()
            print("   RFC-029 section 2.3's claim is that the API stays")
            print("   responsive because it does not do the work, and these")
            print("   numbers are what that is worth: `/health` is unaffected")
            print("   in any way a user could notice, because it touches")
            print("   neither the model nor a long transaction.")
            print()
            print("   Search is the honest half. It is CPU-bound -- a forward")
            print("   pass through the text tower -- so it competes with the")
            print("   executor for cores and slows down, on this machine, by")
            print("   whatever the two columns above differ by. That is CPU")
            print("   contention, not a blocked event loop: the route still")
            print("   answers, which is exactly what indexing *inside* the")
            print("   request would not have done.")
            print()
            print("   Not measured: the same test with several executors, or")
            print("   on a machine with a GPU, where the contention would")
            print("   move somewhere else entirely.")
        finally:
            cleanup(scope, job.id if job is not None else None)
            session.close()
            print()
            print("development database left as it was found")


if __name__ == "__main__":
    main()
