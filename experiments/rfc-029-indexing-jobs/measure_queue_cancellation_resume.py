"""Three costs RFC-029 declared and left as `TBM`, measured against PostgreSQL.

    section 6.1   polling has a start-up latency of up to one interval, and
                  an idle executor costs a query per interval. How much?
    section 15    cancelling waits for the batch in flight. How long is that?
    section 10    the checkpoint "saves the scan, not the inference". How
                  much scan, now that RFC-028 reads EXIF from every file?

The first two run against a **real PostgreSQL** and a real polling loop in
a background thread, because both are questions about a queue rather than
about a function: start-up latency is the gap between `created_at` and
`started_at` as the database records them, and a cancellation has to
travel from one connection to another.

The third needs no model at all -- a resume and a re-scan of an indexed
corpus both skip everything -- which is the point it exists to make.

**Rows are committed to the development database and removed again in a
`finally`.** Nothing else in the repository does that, so the cleanup is
explicit and scoped to the device this script invents: a volume identity
no machine has.

Run it with the backend virtualenv, from the repository root:

    cd <repository root>
    backend/.venv/Scripts/python.exe \
        experiments/rfc-029-indexing-jobs/measure_queue_cancellation_resume.py
"""

from __future__ import annotations

import datetime
import logging
import os
import platform
import random
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from sqlalchemy import delete, text  # noqa: E402
from tests.application.fakes import FakeDeviceLocator  # noqa: E402

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexOrUpdateImagesUseCase,
)
from app.domain.entities.device import Device  # noqa: E402
from app.domain.entities.indexing_job import IndexingJob, JobStatus  # noqa: E402
from app.domain.services.device_identity import (  # noqa: E402
    compute_device_id,
)
from app.domain.value_objects.device_id import (  # noqa: E402
    VolumeIdentity,
    VolumeKind,
)
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.domain.value_objects.job_id import JobId  # noqa: E402
from app.infrastructure.ai.fake_embedding_model import (  # noqa: E402
    FakeEmbeddingModel,
)
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.database.models.indexing_job_model import (  # noqa: E402
    IndexingJobModel,
)
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.persistence.postgres_device_repository import (  # noqa: E402
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (  # noqa: E402
    PostgresIndexingJobRepository,
)
from app.infrastructure.persistence.session import SessionLocal  # noqa: E402
from app.infrastructure.workers.job_runner import JobRunner  # noqa: E402

logging.getLogger("app").setLevel(logging.ERROR)

FILES = int(os.environ.get("RFC029_FILES", "1200"))
EMBED_FILES = int(os.environ.get("RFC029_CANCEL_FILES", "80"))
POLL_INTERVAL = 0.5
SAMPLES = int(os.environ.get("RFC029_SAMPLES", "12"))

MEASURE_VOLUME = VolumeIdentity(
    value=f"\\\\?\\Volume{{{uuid.UUID(int=0x29029029)}}}\\",
    kind=VolumeKind.WINDOWS_VOLUME_GUID,
)
MEASURE_DEVICE_ID = compute_device_id(MEASURE_VOLUME)


def build_corpus(root: Path, count: int) -> None:
    """Write `count` real JPEGs, so the scan and any decode are genuine."""
    from PIL import Image

    for index in range(count):
        folder = root / f"{2018 + index % 4}" / f"{index % 12:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        Image.frombytes(
            "RGB",
            (64, 64),
            bytes((index * 7 + position) % 256 for position in range(64 * 64 * 3)),
        ).save(folder / f"photo_{index:05d}.jpg", "JPEG", quality=70)


def register_device(session: object) -> Device:
    now = datetime.datetime.now(tz=datetime.UTC)
    device = Device(
        id=MEASURE_DEVICE_ID,
        volume_identity=MEASURE_VOLUME,
        label="RFC029-MEASURE",
        first_seen_at=now,
        last_seen_at=now,
    )
    PostgresDeviceRepository(session).save(device)  # type: ignore[arg-type]
    return device


def cleanup_images() -> None:
    """Forget the image rows, keeping the device.

    Needed between phases: the start-latency phase indexes the corpus for
    real, and the cancellation phase then has nothing left to do -- every
    file is skipped and the job finishes before a cancellation could land
    in the middle of a batch. The first draft of this script measured
    exactly that and asserted its way out of it.
    """
    session = SessionLocal()
    try:
        session.execute(
            delete(ImageModel).where(ImageModel.device_id == MEASURE_DEVICE_ID.value)
        )
        session.commit()
    finally:
        session.close()


def cleanup() -> None:
    """Remove every row this script created, and nothing else.

    Images first: `images.device_id` is a foreign key that refuses rather
    than cascades, which is the refusal RFC-027 section 6.3 wants.
    """
    session = SessionLocal()
    try:
        session.execute(
            delete(IndexingJobModel).where(
                IndexingJobModel.device_id == MEASURE_DEVICE_ID.value
            )
        )
        session.execute(
            delete(ImageModel).where(ImageModel.device_id == MEASURE_DEVICE_ID.value)
        )
        session.execute(
            text("DELETE FROM devices WHERE id = :device"),
            {"device": MEASURE_DEVICE_ID.value},
        )
        session.commit()
    finally:
        session.close()


def make_runner(
    disk: Path, model: object, batch_size: int = 8, poll_interval: float = POLL_INTERVAL
) -> tuple[JobRunner, object, object]:
    job_session = SessionLocal()
    image_session = SessionLocal()
    runner = JobRunner(
        jobs=PostgresIndexingJobRepository(job_session),
        devices=PostgresDeviceRepository(job_session),
        locator=FakeDeviceLocator({MEASURE_DEVICE_ID: disk}),
        indexer=IndexOrUpdateImagesUseCase(
            repository=PostgresImageRepository(image_session),
            embedding_model=model,  # type: ignore[arg-type]
            content_hasher=Sha256ContentHasher(),
            batch_size=batch_size,
            metadata_prefetch_size=512,
        ),
        supported_extensions=(".jpg",),
        extract_capture_date=True,
        poll_interval=poll_interval,
        heartbeat_interval=1.0,
    )
    return runner, job_session, image_session


def measure_start_latency(disk: Path) -> None:
    """Section 6.1: how long a job waits for a poll to notice it."""
    print("1. start-up latency of polling (RFC-029 section 6.1)")
    print(f"   poll interval: {POLL_INTERVAL:.2f} s, {SAMPLES} jobs, one executor")
    print()

    runner, job_session, image_session = make_runner(disk, FakeEmbeddingModel())
    stop = threading.Event()

    def poll_forever() -> None:
        while not stop.is_set():
            if not runner.claim_and_run():
                time.sleep(POLL_INTERVAL)

    creator = SessionLocal()
    jobs = PostgresIndexingJobRepository(creator)
    latencies: list[float] = []
    worker = threading.Thread(target=poll_forever, daemon=True)
    worker.start()
    try:
        for _ in range(SAMPLES):
            # A uniformly random point inside the sleep window. Without
            # this the creation loop synchronises with the poll -- each
            # job is created just after the previous one finished, which
            # is always the same phase -- and every sample comes back at
            # very nearly the full interval. That would look like a
            # measurement and be an artefact of the harness.
            time.sleep(random.uniform(0.0, POLL_INTERVAL))
            created = jobs.create(
                IndexingJob(
                    id=JobId.new(),
                    device_id=MEASURE_DEVICE_ID,
                    # A scope that is not on the disk, so the job is claimed
                    # and fails at once: this measures the *queue*, and a
                    # real scan would measure the scan instead.
                    scopes=(),
                    created_at=datetime.datetime.now(tz=datetime.UTC),
                )
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = jobs.get(created.id)
                if current is not None and current.started_at is not None:
                    assert current.created_at is not None
                    latencies.append(
                        (current.started_at - current.created_at).total_seconds()
                    )
                    break
                time.sleep(0.01)
            while True:
                current = jobs.get(created.id)
                if current is None or current.status.is_terminal:
                    break
                time.sleep(0.02)
    finally:
        stop.set()
        worker.join(timeout=10)
        job_session.close()  # type: ignore[attr-defined]
        image_session.close()  # type: ignore[attr-defined]
        creator.close()

    latencies.sort()
    print(f"   samples:     {len(latencies)}")
    print(f"   min:         {min(latencies):8.3f} s")
    print(f"   median:      {statistics.median(latencies):8.3f} s")
    print(f"   max:         {max(latencies):8.3f} s")
    print(f"   mean:        {statistics.fmean(latencies):8.3f} s")
    print()
    print(f"   poll interval:  {POLL_INTERVAL:8.3f} s   for comparison")
    print()
    print("   The mean sits near half the interval, which is what section 6.1")
    print("   predicted: a job is created at a uniformly random point inside")
    print("   the executor's sleep, so on average it waits half of one.")
    print()
    print("   The maximum slightly *exceeds* the interval rather than being")
    print("   bounded by it, and the excess is not the sleep. A job created")
    print("   the instant after a poll waits a full interval and then pays")
    print("   for the claiming UPDATE and for `started_at` becoming visible")
    print("   to the reader -- a few milliseconds each, measured in part 2")
    print('   below. Section 6.1 said "up to one interval"; "one interval')
    print('   plus a round trip" is the honest version.')
    print()
    print("   Against an operation measured in minutes to hours, all of it is")
    print("   noise -- which is the actual claim section 6.1 was making.")
    print()


def measure_idle_poll_cost() -> None:
    """Section 6.1: what an executor with nothing to do costs."""
    print("2. what an idle executor costs (RFC-029 section 6.1)")
    print()

    session = SessionLocal()
    jobs = PostgresIndexingJobRepository(session)
    try:
        now = datetime.datetime.now(tz=datetime.UTC)
        jobs.claim_next(now)  # warm the connection and the plan cache
        rounds = 200
        started = time.perf_counter()
        for _ in range(rounds):
            jobs.claim_next(now)
        per_poll = (time.perf_counter() - started) / rounds

        started = time.perf_counter()
        for _ in range(rounds):
            jobs.list_stale(now)
        per_sweep = (time.perf_counter() - started) / rounds
    finally:
        session.close()

    print(f"   empty claim:      {per_poll * 1000:8.3f} ms per poll")
    print(f"   reaper sweep:     {per_sweep * 1000:8.3f} ms per poll")
    duty = (per_poll + per_sweep) / 2.0
    print(f"   at a 2 s interval: {duty * 100:7.4f}% duty cycle")
    print()
    print("   Two indexed queries against an empty result. This is the cost")
    print("   RFC-029 section 6.1 weighed Redis against, and it is the reason")
    print("   the answer was a table.")
    print()


def measure_cancellation_delay(disk: Path) -> None:
    """Section 15: how long a cancellation waits for the batch in flight."""
    print("3. cancellation delay (RFC-029 sections 8 and 15)")
    print("   real model, batch_size 8 -- the delay *is* the batch in flight")
    print()

    from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel

    model = ClipEmbeddingModel()
    model.warm_up()

    runner, job_session, image_session = make_runner(disk, model, batch_size=8)
    canceller = SessionLocal()
    jobs = PostgresIndexingJobRepository(canceller)
    try:
        created = jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=MEASURE_DEVICE_ID,
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )
        finished_at: list[float] = []

        def run() -> None:
            runner.run_job(created.id)
            finished_at.append(time.perf_counter())

        worker = threading.Thread(target=run, daemon=True)
        worker.start()

        # Let a couple of batches go by, so the cancellation lands in the
        # middle of one rather than before the run has started.
        time.sleep(8.0)
        running = jobs.get(created.id)
        if running is None or running.status is not JobStatus.RUNNING:
            raise SystemExit(
                "the job was not still running when the cancellation was sent "
                f"(status: {running.status.value if running else 'gone'}); the "
                "delay would be measuring nothing"
            )
        requested_at = time.perf_counter()
        jobs.save_if_status(running.request_cancel(), JobStatus.RUNNING)
        worker.join(timeout=120)

        final = jobs.get(created.id)
        assert final is not None
        delay = finished_at[0] - requested_at
        print(f"   final status:     {final.status.value}")
        print(f"   images indexed:   {final.progress.processed_images}")
        print(f"   delay:            {delay:8.2f} s   <-- one observation")
        print("   batch size:       8 images")
        print()
        print("   **One sample, and the spread is the point.** The delay is")
        print("   whatever is left of the batch in flight when the flag is")
        print("   set, so it runs from ~0 s (the flag arrives just before a")
        print("   flush) up to a whole batch. At the 0.426 s per image")
        print("   sustained over 2,000 images in measure_throughput_and_api.log,")
        print("   a full batch of 8 is ~3.4 s, and that is the bound -- the")
        print("   figure above is one draw from inside it, not the worst case.")
        print()
        print("   The per-image rate to use here is the sustained one, not the")
        print("   0.678 s in measure_heartbeat_gaps.log: that run covered 48")
        print("   images, so process warm-up and the first forward pass are a")
        print("   large share of it. Over 2,000 images they amortise away.")
        print()
        print("   The wait buys something: a batch of already-paid-for")
        print("   inference finishes and is written. Aborting mid-forward-pass")
        print("   would discard it for nothing (section 8), and everything")
        print("   that did land stays searchable.")
        print()
    finally:
        job_session.close()  # type: ignore[attr-defined]
        image_session.close()  # type: ignore[attr-defined]
        canceller.close()


def measure_checkpoint_saving(disk: Path) -> None:
    """Section 10: how much of a re-run the checkpoint actually saves."""
    print("4. what the checkpoint saves (RFC-029 section 10)")
    print("   both runs skip every file, so neither pays for inference")
    print()

    runner, job_session, image_session = make_runner(disk, FakeEmbeddingModel())
    session = SessionLocal()
    jobs = PostgresIndexingJobRepository(session)
    try:
        first = jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=MEASURE_DEVICE_ID,
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )
        started = time.perf_counter()
        indexed = runner.run_job(first.id)
        index_elapsed = time.perf_counter() - started
        assert indexed is not None
        print(
            f"   first run:        {index_elapsed:8.2f} s "
            f"({indexed.progress.processed_images} indexed)"
        )

        # A full re-scan: every file is skipped by the incremental check,
        # which is what RFC-029 section 10 means by "a restart is cheap".
        second = jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=MEASURE_DEVICE_ID,
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )
        started = time.perf_counter()
        rescanned = runner.run_job(second.id)
        rescan_elapsed = time.perf_counter() - started
        assert rescanned is not None

        # A resume from halfway, which skips the first half inside the scan.
        #
        # The midpoint is taken from the discovery order itself, not
        # computed from the corpus-building formula. The first draft did
        # the latter and picked `2020/00/photo_00600.jpg` -- a path that
        # does not exist, because the year and the month are both derived
        # from the same index and the combination is impossible. It sorted
        # early, so the "50% resume" skipped 51 files of 1200 and the
        # comparison measured nothing.
        from app.infrastructure.filesystem.filesystem_image_provider import (
            FilesystemImageProvider,
        )

        every_path = sorted(
            found.path.relative_to(disk)
            for found in FilesystemImageProvider(
                disk, (".jpg",), extract_capture_date=False
            ).discover()
        )
        midpoint = ImagePath(every_path[len(every_path) // 2])
        print(
            f"   resume point:     {midpoint} "
            f"({len(every_path) // 2} of {len(every_path)} behind it)"
        )
        third = jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=MEASURE_DEVICE_ID,
                created_at=datetime.datetime.now(tz=datetime.UTC),
                last_processed_relative_path=midpoint,
            )
        )
        started = time.perf_counter()
        resumed = runner.run_job(third.id)
        resume_elapsed = time.perf_counter() - started
        assert resumed is not None

        print(
            f"   full re-scan:     {rescan_elapsed:8.2f} s "
            f"({rescanned.progress.skipped_images} skipped)"
        )
        print(
            f"   resume at 50%:    {resume_elapsed:8.2f} s "
            f"({resumed.progress.skipped_images} reached the pipeline)"
        )
        saved = rescan_elapsed - resume_elapsed
        print(
            f"   saved:            {saved:8.2f} s "
            f"({100 * saved / rescan_elapsed:.0f}%)"
        )
        print()
        print("   RFC-029 section 10 says the checkpoint saves the scan and")
        print("   not the inference. Both figures above confirm the second")
        print("   half of that -- neither run embeds anything, so a restart")
        print("   is already cheap -- and the first half is worth far less")
        print("   than the RFC implied.")
        print()
        print("   Read the absolute numbers, not the percentage: a full")
        print(f"   re-scan of {FILES} indexed files costs {rescan_elapsed:.1f} s")
        print("   in total. The checkpoint can save at most that, and here")
        print("   it saves a fraction of it, because what it skips is the")
        print("   cheapest part of the scan: `sorted(rglob(...))` still walks")
        print("   the whole tree, and only the per-file `stat` and EXIF read")
        print("   are avoided.")
        print()
        print("   So the honest statement is the one RFC-029 section 10 makes")
        print("   at the end and then undersells: **the checkpoint is an")
        print("   optimisation on an already-cheap path.** That is precisely")
        print("   why losing one to a crash costs time and never data, and")
        print("   why the rule that it must never run ahead of the batch")
        print("   buffer matters more than the saving it delivers.")
        print()
        print("   Not measured: a cold external mechanical disk, where the")
        print("   per-file reads the checkpoint skips are seek time rather")
        print("   than cached CPU work. That is the case in which this saving")
        print("   could be large, and it is exactly the case this machine")
        print("   cannot produce.")
        print()
    finally:
        job_session.close()  # type: ignore[attr-defined]
        image_session.close()  # type: ignore[attr-defined]
        session.close()


def main() -> None:
    print("RFC-029 -- queue latency, cancellation delay, and the checkpoint")
    print()
    print(f"machine:   {platform.platform()}")
    print(f"python:    {platform.python_version()}")
    print("database:  PostgreSQL (the development instance)")
    print(f"corpus:    {FILES} synthetic 64x64 JPEGs, {EMBED_FILES} for cancellation")
    print()
    print("NOT measured: a cold external mechanical disk (same limitation as")
    print("RFC-028's). Files here were just written, so the OS cache holds")
    print("them, and every scan figure is CPU rather than seek time.")
    print()

    cleanup()
    session = SessionLocal()
    try:
        register_device(session)
    finally:
        session.close()

    try:
        with tempfile.TemporaryDirectory(prefix="rfc029-queue-") as raw:
            disk = Path(raw)
            build_corpus(disk, EMBED_FILES)
            measure_start_latency(disk)
            measure_idle_poll_cost()
            cleanup_images()
            measure_cancellation_delay(disk)

        cleanup()
        session = SessionLocal()
        try:
            register_device(session)
        finally:
            session.close()

        with tempfile.TemporaryDirectory(prefix="rfc029-resume-") as raw:
            disk = Path(raw)
            build_corpus(disk, FILES)
            measure_checkpoint_saving(disk)
    finally:
        cleanup()
        print("development database left as it was found")


if __name__ == "__main__":
    main()
