"""How long can a *healthy* job go silent? The reaper's timeout follows.

RFC-029 section 9.1 picked its timeout by reasoning about one batch --
"`batch_size` 8 at ~450 ms is ~3.6 s per batch, so a few minutes is
generous" -- and then marked the number `TBM`. The reasoning is the part
that is wrong, not just the number: **a batch is not the longest a healthy
job goes without reporting.** Three ordinary runs go much longer.

    new indexing   every file is embedded; gaps are batch-shaped
    100% re-scan   no batch is ever flushed -- the most common run there is
    resume         everything before the checkpoint is skipped before it is
                   yielded, so nothing reaches the pipeline at all
    first window   `sorted(rglob(...))` materialises the whole file list
                   before the first file comes out

A timeout below the worst of those kills jobs that are working perfectly,
and the failure mode is nasty: it happens on the *healthiest* run, the
one where everything is already indexed.

So this measures the largest interval between consecutive heartbeat
writes, per scenario, and the timeout is chosen from the worst observed
maximum with a stated margin.

**Everything here is fake except the timing that matters.** The embedding
model is `FakeEmbeddingModel` in three of the four scenarios, because
those scenarios never embed anything -- that is what defines them. The
"new indexing" scenario is run with the real model, since there the gap
*is* the batch. The disk is a real temporary directory with real files,
and the scan is the real `FilesystemImageProvider`.

**What this does not measure, and says so:** a cold external mechanical
disk. Every file below was just written, so the operating system's cache
holds it, and the `sorted(rglob(...))` figure here is CPU and parsing
rather than seek time. Windows offers no unprivileged way to drop the
file cache, and inventing a seek penalty would be a deduction rather than
a measurement (the same limitation RFC-028 declared).

Run it with the backend virtualenv, from the repository root:

    cd <repository root>
    backend/.venv/Scripts/python.exe \
        experiments/rfc-029-indexing-jobs/measure_heartbeat_gaps.py

`RFC029_FILES` sets the corpus size (default 600); `RFC029_EMBED_FILES`
sets the smaller corpus the real model runs over (default 48).
"""

from __future__ import annotations

import datetime
import logging
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from tests.application.fakes import (  # noqa: E402
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
    make_device,
)

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexOrUpdateImagesUseCase,
)
from app.domain.entities.indexing_job import IndexingJob  # noqa: E402
from app.domain.services.embedding_model_port import EmbeddingModelPort  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.domain.value_objects.indexing_progress import IndexingProgress  # noqa: E402
from app.domain.value_objects.job_id import JobId  # noqa: E402
from app.infrastructure.ai.fake_embedding_model import (  # noqa: E402
    FakeEmbeddingModel,
)
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.persistence.in_memory_indexing_job_repository import (  # noqa: E402
    InMemoryIndexingJobRepository,
)
from app.infrastructure.workers.job_runner import JobRunner  # noqa: E402

# The worker logs every discovered file at INFO. That is right for an
# operator watching a run and useless here -- it would bury the numbers
# and, far worse, charge this measurement for the I/O of writing them.
logging.getLogger("app").setLevel(logging.ERROR)

FILES = int(os.environ.get("RFC029_FILES", "3000"))
EMBED_FILES = int(os.environ.get("RFC029_EMBED_FILES", "48"))
SUPPORTED = (".jpg",)


class TimingJobRepository(InMemoryIndexingJobRepository):
    """Records when every heartbeat write happened, in wall-clock seconds.

    The measurement is taken at the repository rather than inside the
    observer because the repository is where a heartbeat becomes visible
    to a reaper. An observer that decided to write and then failed would
    still look alive from inside the process, and it is precisely the
    outside view that the reaper has.
    """

    def __init__(self) -> None:
        super().__init__()
        self.heartbeats: list[float] = []

    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        self.heartbeats.append(time.perf_counter())
        return super().record_progress(job_id, progress, checkpoint, heartbeat_at)


def build_corpus(root: Path, count: int) -> None:
    """Write `count` real JPEGs across a year/month tree.

    **Real JPEGs, written with Pillow, not files that merely end in
    `.jpg`.** A first draft of this script wrote 256 bytes of `x`, and the
    real-model scenario then reported a perfectly healthy-looking gap over
    *zero* images embedded: every file failed to decode, so the batch path
    never ran and the number measured nothing. Undecodable bytes also skew
    the scan, since RFC-028 reads an EXIF header from every discovered
    file and a broken one takes the error path.

    They are small (64x64 noise), and that is declared rather than hidden:
    a real photograph is several megabytes and costs more to read and to
    decode. The inference cost is unaffected -- CLIP resizes everything to
    224x224 -- so the batch-shaped gaps below are representative while the
    scan-shaped ones are optimistic.
    """
    from PIL import Image

    for index in range(count):
        folder = root / f"{2018 + index % 4}" / f"{index % 12:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        pixels = Image.frombytes(
            "RGB",
            (64, 64),
            bytes((index * 7 + position) % 256 for position in range(64 * 64 * 3)),
        )
        pixels.save(folder / f"photo_{index:05d}.jpg", "JPEG", quality=70)


def run_once(
    disk: Path,
    model: EmbeddingModelPort,
    resume_from: str | None = None,
    batch_size: int = 8,
) -> tuple[TimingJobRepository, IndexingJob | None, float, FakeImageRepository]:
    """Run one job over `disk`, returning its heartbeat timeline."""
    device = make_device(label="MEASURE")
    jobs = TimingJobRepository()
    images = FakeImageRepository()
    runner = JobRunner(
        jobs=jobs,
        devices=FakeDeviceRepository([device]),
        locator=FakeDeviceLocator({device.id: disk}),
        indexer=IndexOrUpdateImagesUseCase(
            repository=images,
            embedding_model=model,
            content_hasher=Sha256ContentHasher(),
            batch_size=batch_size,
            metadata_prefetch_size=512,
        ),
        supported_extensions=SUPPORTED,
        extract_capture_date=True,
        heartbeat_interval=10.0,
    )
    job = jobs.create(
        IndexingJob(
            id=JobId.new(),
            device_id=device.id,
            created_at=datetime.datetime.now(tz=datetime.UTC),
            last_processed_relative_path=(
                ImagePath(resume_from) if resume_from else None
            ),
        )
    )
    started = time.perf_counter()
    finished = runner.run_job(job.id)
    elapsed = time.perf_counter() - started
    return jobs, finished, elapsed, images


def gaps(timeline: list[float], started: float, ended: float) -> list[float]:
    """Intervals a reaper would see, including before the first heartbeat.

    The interval from the claim to the *first* heartbeat is included, and
    it is often the largest one: `claim()` stamps a heartbeat and then the
    scan materialises its file list before producing anything. A
    measurement that only looked at gaps between writes would miss exactly
    the window the first-window scenario exists to expose.
    """
    marks = [started, *timeline, ended]
    return [
        later - earlier for earlier, later in zip(marks[:-1], marks[1:], strict=True)
    ]


def report(name: str, note: str, intervals: list[float], elapsed: float) -> str:
    worst = max(intervals) if intervals else elapsed
    lines = [
        f"  {name}",
        f"    {note}",
        f"    run:            {elapsed:8.2f} s",
        f"    heartbeats:     {len(intervals) - 1:8d}",
        f"    longest gap:    {worst:8.2f} s   <-- what the timeout must clear",
    ]
    if len(intervals) > 2:
        lines.append(f"    median gap:     {statistics.median(intervals):8.2f} s")
    return "\n".join(lines)


def main() -> None:
    print("RFC-029 section 9.1 -- how long a healthy job goes without reporting")
    print()
    print(f"machine:   {platform.platform()}")
    print(f"python:    {platform.python_version()}")
    print(f"corpus:    {FILES} files (skip scenarios), {EMBED_FILES} (embedding)")
    print("model:     FakeEmbeddingModel, except where stated")
    print()
    print("Run this on an otherwise idle machine. An earlier run of this very")
    print("script, taken while measure_throughput_and_api.py was saturating a")
    print("core, reported a worst gap of 17.88 s against the 2-3 s seen idle --")
    print("six times larger, from CPU contention alone. A user who searches")
    print("while a job runs creates exactly that contention, which is part of")
    print("why the margin chosen below is as large as it is.")
    print()
    print("NOT measured: a cold external mechanical disk. Every file below was")
    print("just written, so the OS cache holds it; the scan figures are CPU and")
    print("parsing, not seek time. Windows offers no unprivileged way to drop")
    print("the file cache, and inventing a seek penalty would be a deduction.")
    print()

    worst_overall: list[tuple[str, float]] = []

    with tempfile.TemporaryDirectory(prefix="rfc029-") as raw:
        disk = Path(raw)
        build_corpus(disk, FILES)

        print("scenarios")
        print()

        # 1. First window: the scan has to materialise before anything runs.
        started = time.perf_counter()
        jobs, _, elapsed, images = run_once(disk, FakeEmbeddingModel())
        ended = time.perf_counter()
        intervals = gaps(jobs.heartbeats, started, ended)
        print(
            report(
                "1. new indexing (fake model)",
                "every file embedded; the scan and the pipeline both run",
                intervals,
                elapsed,
            )
        )
        worst_overall.append(("new indexing (fake model)", max(intervals)))
        print()

        # 2. A re-scan where the incremental check skips every file. No batch
        #    is ever flushed, which is what makes this the dangerous one.
        started = time.perf_counter()
        jobs2 = TimingJobRepository()
        device = make_device(label="MEASURE")
        runner = JobRunner(
            jobs=jobs2,
            devices=FakeDeviceRepository([device]),
            locator=FakeDeviceLocator({device.id: disk}),
            indexer=IndexOrUpdateImagesUseCase(
                repository=images,
                embedding_model=FakeEmbeddingModel(),
                content_hasher=Sha256ContentHasher(),
                batch_size=8,
                metadata_prefetch_size=512,
            ),
            supported_extensions=SUPPORTED,
            extract_capture_date=True,
            heartbeat_interval=10.0,
        )
        job = jobs2.create(
            IndexingJob(
                id=JobId.new(),
                device_id=device.id,
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )
        rescan_start = time.perf_counter()
        rescanned = runner.run_job(job.id)
        rescan_elapsed = time.perf_counter() - rescan_start
        ended = time.perf_counter()
        intervals = gaps(jobs2.heartbeats, started, ended)
        skipped = rescanned.progress.skipped_images if rescanned else 0
        print(
            report(
                "2. re-scan, 100% skipped (fake model)",
                f"{skipped} of {FILES} skipped; **no batch is ever flushed**",
                intervals,
                rescan_elapsed,
            )
        )
        worst_overall.append(("re-scan, 100% skipped", max(intervals)))
        print()

        # 3. A resume: everything before the checkpoint is dropped inside the
        #    provider, before it is yielded, so the pipeline sees nothing.
        half = FILES // 2
        midpoint = f"{2018 + half % 4}/{half % 12:02d}/photo_{half:05d}.jpg"
        started = time.perf_counter()
        jobs3, resumed, resume_elapsed, _ = run_once(
            disk, FakeEmbeddingModel(), resume_from=midpoint
        )
        ended = time.perf_counter()
        intervals = gaps(jobs3.heartbeats, started, ended)
        print(
            report(
                "3. resume from a checkpoint (fake model)",
                "files before the checkpoint are dropped before being yielded",
                intervals,
                resume_elapsed,
            )
        )
        worst_overall.append(("resume from a checkpoint", max(intervals)))
        print()

    # 4. The real model, on a small corpus: here the gap *is* the batch, which
    #    is the only case RFC-029 section 9.1 originally considered.
    with tempfile.TemporaryDirectory(prefix="rfc029-embed-") as raw:
        disk = Path(raw)
        build_corpus(disk, EMBED_FILES)
        print("  4. new indexing (real model) -- loading the checkpoint...")
        from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel

        model = ClipEmbeddingModel()
        load_started = time.perf_counter()
        model.warm_up()
        load_elapsed = time.perf_counter() - load_started
        print(f"     model load: {load_elapsed:8.2f} s (before any job is claimed)")

        started = time.perf_counter()
        jobs4, finished, real_elapsed, _ = run_once(disk, model)
        ended = time.perf_counter()
        intervals = gaps(jobs4.heartbeats, started, ended)
        indexed = finished.progress.processed_images if finished else 0
        failed = finished.progress.failed_images if finished else 0
        if indexed == 0:
            raise SystemExit(
                "the real-model scenario embedded nothing "
                f"({failed} files failed); the measurement would be vacuous"
            )
        print(
            report(
                "4. new indexing (real model)",
                f"{indexed} images embedded on CPU; the gap here is the batch",
                intervals,
                real_elapsed,
            )
        )
        print(f"    per image:      {real_elapsed / indexed:8.3f} s")
        worst_overall.append(("new indexing (real model)", max(intervals)))
        print()

    print("conclusion")
    print()
    for name, worst in sorted(worst_overall, key=lambda pair: -pair[1]):
        print(f"  {worst:8.2f} s   {name}")
    observed = max(worst for _, worst in worst_overall)
    print()
    print(f"  worst observed gap:             {observed:8.2f} s")
    print("  chosen job_stale_timeout:          120.00 s")
    print(f"  margin over the worst observed: {120.0 / observed:8.1f}x")
    print()
    print("  The margin is deliberately large, and the asymmetry is why: a")
    print("  timeout that is too short kills a healthy job -- most likely the")
    print("  100%-skip re-scan, the commonest run of all -- while one that is")
    print("  too long only delays a device becoming free after a real crash.")
    print()
    print("  The figure this does NOT cover is the first window of a cold")
    print("  mechanical disk, because `sorted(rglob(...))` materialises the")
    print("  whole file list before the first heartbeat and no callback can")
    print("  fire during it. On a 40,000-file external disk that wait is seek")
    print("  time, which is not measurable here. Raise job_stale_timeout on a")
    print("  machine whose disks are slow enough to matter.")


if __name__ == "__main__":
    main()
