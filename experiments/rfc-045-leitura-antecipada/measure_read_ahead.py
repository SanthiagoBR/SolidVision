"""RFC-045 section 10: the read-ahead and the draft, measured as they ship.

Unlike the eight pilots in `experiments/thumbnails-in-indexing/`, nothing here
re-implements the pipeline: every job is run by the `JobRunner` that
`build_runner()` composes -- `ReadAheadCandidates`, the use case, the
PostgreSQL repositories, the CLIP adapter with its draft, the thumbnails --
against an **embedded PostgreSQL 16 of its own**, created in a temporary
directory and removed at the end. The development database is not touched.
Nothing is written to the photos.

One thing is replaced, as the pilots replaced it: the scan. A job indexes
folders, and a measurement wants groups of cold photos drawn at random from
the whole disk, so `FilesystemImageProvider` is swapped for one that yields
a fixed list of files -- with the same `stat`, the same EXIF read and the
same observer calls the real one makes, in path order.

Steps, each of them a row of section 10:

    first      first indexing, off / on (32) / on (64), alternated in cold
               groups of 192, with thumbnails: s/img, the projection for
               the archive, the peak working set, the metadata queries,
               the wait for the next file
    rescan     2,000 photos already indexed: a priming pass, then off / on
               alternated; and contents_prefetched
    touched    10% of them with the stored mtime moved back -- the scan then
               sees them as touched, as if their date had changed; the
               photos themselves are never written to (RFC-028 section 10)
    cancel     a cancellation requested mid-job, off / on alternated: the
               seconds from the request to `cancelled`, and no thread alive
    snapshot   `list_change_signals()` over 116,396 rows with the real
               paths of the disk: seconds and MB
    vectors    50 photos of the disk: bytes against path (difference 0),
               and the cosine of the draft's vectors with today's
    demo       the four numbers of `test_semantic_search_e2e.py` with the
               production adapter, and the RFC-035 reference vectors
    unplug     not run by default: a job that waits for the disk to be
               unplugged by hand and times how it ends
    restart    not run by default, because it is the one step that changes
               the development database: its census, the migration of
               section 5.9 applied by the cluster's own `prepare()`, the
               census again, the demo reindexed by the CLI's composition,
               and the four numbers searched in that database. Back the
               database up first

Run it from the repository root, with the disk plugged in:

    .venv/Scripts/python.exe experiments/rfc-045-leitura-antecipada/measure_read_ahead.py \\
        --root E:/ > experiments/rfc-045-leitura-antecipada/measure_read_ahead.log
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import gc
import json
import math
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
import uuid
from collections.abc import Callable, Iterator
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
sys.path.insert(0, str(REPOSITORY_ROOT))

import numpy as np  # noqa: E402
import psycopg  # noqa: E402
from PIL.JpegImagePlugin import JpegImageFile  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.application.use_cases.cancel_indexing_job import (  # noqa: E402
    CancelIndexingJobUseCase,
)
from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexingSummary,
)
from app.domain.entities.device import Device  # noqa: E402
from app.domain.entities.image import Image  # noqa: E402
from app.domain.entities.indexing_job import IndexingJob  # noqa: E402
from app.domain.services.indexing_observer import IndexingObserver  # noqa: E402
from app.domain.value_objects.device_id import (  # noqa: E402
    DeviceId,
    VolumeIdentity,
    VolumeKind,
)
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.domain.value_objects.indexing_progress import IndexingProgress  # noqa: E402
from app.domain.value_objects.job_id import JobId  # noqa: E402
from app.infrastructure.config.settings import settings  # noqa: E402
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres  # noqa: E402
from app.infrastructure.database.embedded.cluster_home import ClusterHome  # noqa: E402
from app.infrastructure.database.embedded.runtime import PostgresRuntime  # noqa: E402
from app.infrastructure.filesystem.discovered_image_file import (  # noqa: E402
    DiscoveredImageFile,
)
from app.infrastructure.filesystem.exif_capture_date import (  # noqa: E402
    read_exif_facts,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    discovery_sort_key,
)
from app.infrastructure.filesystem.image_identity import compute_image_id  # noqa: E402
from app.infrastructure.filesystem.volume_identity_provider import (  # noqa: E402
    WindowsVolumeIdentityProvider,
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
from app.infrastructure.workers import job_runner as job_runner_module  # noqa: E402
from app.infrastructure.workers.indexing_worker import register_device  # noqa: E402
from app.infrastructure.workers.job_progress_observer import (  # noqa: E402
    JobProgressObserver,
)
from app.infrastructure.workers.read_ahead import ReadAheadCandidates  # noqa: E402

SEED = 20261029
"""20261028 was the seed of a first run, stopped after four groups: its engine had
no connection pool, so every transaction after a commit opened a new server
process -- which the job runner's engine never does."""
GROUP = 192
ARCHIVE_FILES = 116_396
"""The supported files of `E:` on 28/10 (RFC-045 section 1): the projection's base."""
MB = 1e6

# ---------------------------------------------------------------------------
# The scan, replaced by a fixed list of files
# ---------------------------------------------------------------------------


class SampledProvider:
    """`FilesystemImageProvider`'s signature, over `SampledProvider.paths`.

    The body is `_discover_under()`'s for one file -- `stat`, then the EXIF
    read of the capture date and the position -- in path order, telling the
    observer as the real scan does: `listed()` first, `discovered()` every
    200 files and once at the end.
    """

    paths: list[Path] = []
    seconds = 0.0

    def __init__(
        self,
        root: Path,
        supported_extensions: object,
        extract_capture_date: bool = True,
        scopes: object = (),
        resume_after: PurePath | None = None,
        observer: IndexingObserver | None = None,
        report_every: int = 200,
        excluded_directories: object = (),
        extract_gps: bool = True,
    ) -> None:
        self._root = root
        self._observer = observer
        self._capture = extract_capture_date
        self._gps = extract_gps
        self._report_every = report_every

    def discover(self) -> Iterator[DiscoveredImageFile]:
        ordered = sorted(
            SampledProvider.paths,
            key=lambda path: discovery_sort_key(path.relative_to(self._root)),
        )
        if self._observer is not None:
            self._observer.listed(len(ordered))
        for index, path in enumerate(ordered, start=1):
            started = time.perf_counter()
            stat = path.stat()
            facts = read_exif_facts(
                path, capture_date=self._capture, position=self._gps
            )
            SampledProvider.seconds += time.perf_counter() - started
            yield DiscoveredImageFile(
                path=path,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                file_size=stat.st_size,
                file_modified_at=datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ),
                capture_date=facts.capture_date,
                position=facts.position,
            )
            if self._observer is not None and index % self._report_every == 0:
                self._observer.discovered(index, complete=False)
        if self._observer is not None:
            self._observer.discovered(len(ordered), complete=True)


# ---------------------------------------------------------------------------
# What the runner does not report by itself
# ---------------------------------------------------------------------------


@dataclass
class Timeline:
    """When the first metadata window ended, and how many files it settled."""

    first_window_at: float | None = None
    settled_at_first_window: int = 0


TIMELINE = Timeline()
_ORIGINAL_WINDOW_DECIDED = JobProgressObserver.window_decided
_ORIGINAL_PROVIDER = job_runner_module.FilesystemImageProvider
_ORIGINAL_READ_AHEAD = job_runner_module.ReadAheadCandidates


def _recording_window_decided(
    self: JobProgressObserver,
    progress: IndexingProgress,
    durable_through: ImagePath | None,
) -> None:
    if TIMELINE.first_window_at is None:
        TIMELINE.first_window_at = time.perf_counter()
        TIMELINE.settled_at_first_window = (
            progress.processed_images + progress.skipped_images + progress.failed_images
        )
    _ORIGINAL_WINDOW_DECIDED(self, progress, durable_through)


class RecordedReadAhead(ReadAheadCandidates):
    """The production class, remembering the last instance for its counters."""

    last: RecordedReadAhead | None = None

    def __enter__(self) -> RecordedReadAhead:
        RecordedReadAhead.last = self
        super().__enter__()
        return self


class TimedRepository:
    """The image repository the runner was given, timing two of its reads."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.metadata_seconds = 0.0
        self.metadata_queries = 0
        self.snapshot_seconds = 0.0
        self.snapshot_rows = 0

    def get_index_metadata_many(self, image_ids: Any) -> Any:
        started = time.perf_counter()
        try:
            return self._inner.get_index_metadata_many(image_ids)
        finally:
            self.metadata_seconds += time.perf_counter() - started
            self.metadata_queries += 1

    def list_change_signals(self, device_id: Any) -> Any:
        started = time.perf_counter()
        signals = self._inner.list_change_signals(device_id)
        self.snapshot_seconds += time.perf_counter() - started
        self.snapshot_rows = len(signals)
        return signals

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
_KERNEL32.K32GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_MemoryCounters),
    wintypes.DWORD,
]
_KERNEL32.K32GetProcessMemoryInfo.restype = wintypes.BOOL


def memory_now() -> tuple[int, int]:
    """This process's working set and private bytes."""
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not _KERNEL32.K32GetProcessMemoryInfo(
        _KERNEL32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return counters.WorkingSetSize, counters.PrivateUsage


class MemorySampler:
    """The peak working set and private bytes over a block, sampled every 50 ms."""

    def __init__(self) -> None:
        self.peak_working_set = 0
        self.peak_private = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> MemorySampler:
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    def _sample(self) -> None:
        working_set, private = memory_now()
        self.peak_working_set = max(self.peak_working_set, working_set)
        self.peak_private = max(self.peak_private, private)

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            self._sample()


FREQUENCY = r"\Informações do Processador(_Total)\Frequência do Processador"
PERFORMANCE = r"\Informações do Processador(_Total)\% de Desempenho do Processador"


class ClockSampler:
    """The CPU's effective MHz once a second, from a PowerShell child (as the pilots)."""

    def __init__(self, output: Path) -> None:
        self._output = output
        self._process: Any = None

    def __enter__(self) -> ClockSampler:
        import base64
        import subprocess

        script = (
            f"$out = '{self._output}'\n"
            f"Get-Counter -Counter @('{PERFORMANCE}','{FREQUENCY}') -SampleInterval 1 "
            "-Continuous | ForEach-Object {\n"
            "  $t = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()\n"
            "  $v = foreach ($s in $_.CounterSamples) "
            "{ $s.CookedValue.ToString([Globalization.CultureInfo]::InvariantCulture) }\n"
            "  Add-Content -Path $out -Value (\"$t;\" + ($v -join ';'))\n"
            "}\n"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        self._process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.wait()

    def mean_mhz(self, start: float, end: float) -> float | None:
        if not self._output.exists():
            return None
        values = []
        for line in self._output.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            parts = line.strip().split(";")
            if len(parts) != 3:
                continue
            try:
                stamp = int(parts[0]) / 1000.0
                performance, frequency = float(parts[1]), float(parts[2])
            except ValueError:
                continue
            if start <= stamp <= end:
                values.append(performance * frequency / 100.0)
        return statistics.fmean(values) if values else None


def reader_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "solidvision-read-ahead"]


# ---------------------------------------------------------------------------
# The world: a cluster of its own, the device, the runner
# ---------------------------------------------------------------------------


def production_engine(connection: Any) -> Any:
    """An engine like the job runner's: SQLAlchemy's pool, with `pool_pre_ping`.

    `build_engine()` itself reads the cluster from the settings, which point at
    the development database; this one points at the measurement's own.
    """
    return create_engine(connection.url(), pool_pre_ping=True)


class World:
    def __init__(self, root: Path, scratch: Path) -> None:
        self.root = root
        self.scratch = scratch
        runtime = PostgresRuntime(settings.embedded_postgres_runtime_directory)
        self.cluster = EmbeddedPostgres(
            runtime, ClusterHome(scratch / "SolidVision" / "postgres")
        )
        started = time.perf_counter()
        self.connection = self.cluster.start().connection
        self.cluster.prepare(self.connection)
        print(
            f"embedded PostgreSQL 16 ready in {time.perf_counter() - started:.1f} s "
            f"(port {self.connection.port})",
            flush=True,
        )
        self.engine = production_engine(self.connection)
        # The repositories' engine singleton is not used: every session below
        # is bound to this cluster's engine explicitly.
        self.job_session = Session(bind=self.engine)
        self.image_session = Session(bind=self.engine)
        self.device, volume = register_device(
            WindowsVolumeIdentityProvider(),
            PostgresDeviceRepository(self.job_session),
            root=root,
            label="E",
        )
        self.mount = volume.mount_point
        self.jobs = PostgresIndexingJobRepository(self.job_session)
        settings.thumbnail_directory = scratch / "thumbnails"  # type: ignore[misc]

    def runner(self, read_ahead_files: int | None) -> tuple[Any, TimedRepository]:
        settings.indexing_read_ahead = read_ahead_files is not None  # type: ignore[misc]
        if read_ahead_files is not None:
            settings.read_ahead_files = read_ahead_files  # type: ignore[misc]
        runner = job_runner_module.build_runner(self.job_session, self.image_session)
        timed = TimedRepository(runner._indexer._repository)
        runner._indexer._repository = timed
        if runner._images is not None:
            runner._images = timed
        return runner, timed

    def new_job(self) -> IndexingJob:
        return self.jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=self.device.id,
                scopes=(),
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )

    def close(self) -> None:
        self.job_session.close()
        self.image_session.close()
        self.engine.dispose()
        self.cluster.stop()


@dataclass
class JobResult:
    step: str
    mode: str
    files: int
    seconds: float
    seconds_per_image: float
    steady_seconds_per_image: float
    status: str
    indexed: int = 0
    skipped_unchanged: int = 0
    skipped_content_identical: int = 0
    failed: int = 0
    contents_prefetched: int = 0
    contents_read_late: int = 0
    candidate_wait_seconds: float = 0.0
    inference_seconds: float = 0.0
    thumbnail_seconds: float = 0.0
    persistence_seconds: float = 0.0
    discovery_seconds: float = 0.0
    metadata_seconds: float = 0.0
    metadata_queries: int = 0
    snapshot_seconds: float = 0.0
    snapshot_rows: int = 0
    peak_working_set_mb: float = 0.0
    peak_private_mb: float = 0.0
    read_ahead_peak_mb: float = 0.0
    read_ahead_read: int = 0
    read_ahead_over_budget: int = 0
    read_ahead_too_large: int = 0
    mhz: float | None = None
    threads_alive_after: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def mode_name(files: int | None) -> str:
    return "off" if files is None else f"on/{files}"


def run_job(
    world: World,
    step: str,
    read_ahead_files: int | None,
    paths: list[Path],
    clock: ClockSampler | None,
    during: Callable[[IndexingJob], None] | None = None,
) -> JobResult:
    runner, timed = world.runner(read_ahead_files)
    captured: dict[str, IndexingSummary] = {}
    execute = runner._indexer.execute

    def capturing(candidates: Any, observer: Any = None) -> IndexingSummary:
        summary = execute(candidates, observer)
        captured["summary"] = summary
        return summary

    runner._indexer.execute = capturing
    SampledProvider.paths = paths
    SampledProvider.seconds = 0.0
    RecordedReadAhead.last = None
    TIMELINE.first_window_at = None
    TIMELINE.settled_at_first_window = 0
    job = world.new_job()
    gc.collect()

    wall_start = time.time()
    if during is not None:
        during(job)
    started = time.perf_counter()
    with MemorySampler() as memory:
        finished_job = runner.run_job(job.id)
    finished = time.perf_counter()
    wall_end = time.time()
    alive = len(reader_threads())

    summary = captured.get("summary", IndexingSummary())
    settled = (
        summary.indexed
        + summary.failed
        + summary.skipped_unchanged
        + (summary.skipped_content_identical)
    )
    if (
        TIMELINE.first_window_at is not None
        and settled > TIMELINE.settled_at_first_window
    ):
        steady = (finished - TIMELINE.first_window_at) / (
            settled - TIMELINE.settled_at_first_window
        )
    else:
        steady = (finished - started) / max(1, len(paths))
    read_ahead = RecordedReadAhead.last
    result = JobResult(
        step=step,
        mode=mode_name(read_ahead_files),
        files=len(paths),
        seconds=finished - started,
        seconds_per_image=(finished - started) / max(1, len(paths)),
        steady_seconds_per_image=steady,
        status=finished_job.status.value if finished_job is not None else "lost",
        indexed=summary.indexed,
        skipped_unchanged=summary.skipped_unchanged,
        skipped_content_identical=summary.skipped_content_identical,
        failed=summary.failed,
        contents_prefetched=summary.contents_prefetched,
        contents_read_late=summary.contents_read_late,
        candidate_wait_seconds=summary.candidate_wait_seconds,
        inference_seconds=summary.inference_seconds,
        thumbnail_seconds=summary.thumbnail_seconds,
        persistence_seconds=summary.persistence_seconds,
        discovery_seconds=SampledProvider.seconds,
        metadata_seconds=timed.metadata_seconds,
        metadata_queries=timed.metadata_queries,
        snapshot_seconds=timed.snapshot_seconds,
        snapshot_rows=timed.snapshot_rows,
        peak_working_set_mb=memory.peak_working_set / MB,
        peak_private_mb=memory.peak_private / MB,
        read_ahead_peak_mb=(read_ahead.budget.peak / MB) if read_ahead else 0.0,
        read_ahead_read=read_ahead.stats.read if read_ahead else 0,
        read_ahead_over_budget=read_ahead.stats.over_budget if read_ahead else 0,
        read_ahead_too_large=read_ahead.stats.too_large if read_ahead else 0,
        mhz=clock.mean_mhz(wall_start, wall_end) if clock else None,
        threads_alive_after=alive,
        extra={"finished_at": finished},
    )
    print(
        f"  {step:<8} {result.mode:<6} {result.files:>5} files  {result.status:<9} "
        f"{result.seconds:7.1f} s  {result.seconds_per_image:6.3f} s/img  "
        f"steady {result.steady_seconds_per_image:6.3f}  "
        f"wait {1000 * result.candidate_wait_seconds / max(1, result.files):5.0f} ms/img  "
        f"infer {1000 * result.inference_seconds / max(1, result.indexed):4.0f}  "
        f"thumb {1000 * result.thumbnail_seconds / max(1, result.indexed):4.0f}  "
        f"meta {result.metadata_queries:3d}q {result.metadata_seconds:5.2f} s  "
        f"WS {result.peak_working_set_mb:6.0f} MB  held {result.read_ahead_peak_mb:5.0f} MB  "
        f"pref {result.contents_prefetched:4d} late {result.contents_read_late:4d}  "
        f"MHz {result.mhz or 0:5.0f}  threads {alive}",
        flush=True,
    )
    return result


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def step_first(
    world: World, pool: list[Path], clock: ClockSampler, cycles: int
) -> list[JobResult]:
    print(
        f"\nfirst indexing: {cycles} cycles of off / on/32 / on/64, {GROUP} cold photos each",
        flush=True,
    )
    results = []
    for cycle in range(cycles):
        order = [None, 32, 64] if cycle % 2 == 0 else [64, 32, None]
        for files in order:
            group = [pool.pop() for _ in range(GROUP)]
            results.append(run_job(world, "first", files, group, clock))
    return results


def indexed_paths(world: World) -> list[Path]:
    with world.engine.connect() as db:
        rows = db.execute(
            text("SELECT relative_path FROM images WHERE device_id = :device"),
            {"device": world.device.id.value},
        ).all()
    return [world.mount / row[0] for row in rows]


def step_rescan(
    world: World, clock: ClockSampler, count: int
) -> tuple[list[JobResult], list[Path]]:
    indexed = indexed_paths(world)
    random.Random(SEED + 1).shuffle(indexed)
    chosen = indexed[:count]
    print(
        f"\nre-scan of {len(chosen)} indexed photos: priming, then off / on alternated",
        flush=True,
    )
    results = [run_job(world, "prime", None, chosen, clock)]
    for files in (None, 32, None, 32):
        results.append(run_job(world, "rescan", files, chosen, clock))
    return results, chosen


def step_touched(world: World, clock: ClockSampler, chosen: list[Path]) -> JobResult:
    touched = chosen[: len(chosen) // 10]
    ids = [
        compute_image_id(
            world.device.id, ImagePath(path.relative_to(world.mount))
        ).value
        for path in touched
    ]
    with world.engine.begin() as db:
        moved = db.execute(
            text(
                "UPDATE images SET file_modified_at = file_modified_at - interval '1 hour' "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        ).rowcount
    print(
        f"\nre-scan with {moved} of {len(chosen)} rows touched (stored mtime moved back)",
        flush=True,
    )
    result = run_job(world, "touched", 32, chosen, clock)
    result.extra["touched"] = moved
    return result


def step_cancel(
    world: World,
    pool: list[Path],
    clock: ClockSampler,
    repetitions: int,
    delays: list[float],
) -> list[JobResult]:
    results = []
    for after in delays:
        results.extend(_cancel_at(world, pool, clock, repetitions, after))
    return results


def _cancel_at(
    world: World, pool: list[Path], clock: ClockSampler, repetitions: int, after: float
) -> list[JobResult]:
    print(
        f"\ncancellation {after:.0f} s into a first indexing of 300 cold photos, off / on alternated",
        flush=True,
    )
    results = []
    for repetition in range(repetitions):
        for files in ((None, 32) if repetition % 2 == 0 else (32, None)):
            group = [pool.pop() for _ in range(300)]
            requested: dict[str, float] = {}

            def arm_cancel(job: IndexingJob) -> None:
                def cancel() -> None:
                    with Session(bind=world.engine) as session:
                        CancelIndexingJobUseCase(
                            job_repository=PostgresIndexingJobRepository(session)
                        ).execute(job.id)
                    requested["at"] = time.perf_counter()

                timer = threading.Timer(after, cancel)
                timer.daemon = True
                timer.start()

            result = run_job(world, "cancel", files, group, clock, during=arm_cancel)
            if "at" in requested:
                result.extra["request_to_end_seconds"] = (
                    result.extra["finished_at"] - requested["at"]
                )
                print(
                    f"           request -> {result.status}: "
                    f"{result.extra['request_to_end_seconds']:.2f} s",
                    flush=True,
                )
            else:
                result.extra["request_to_end_seconds"] = None
                print("           the job ended before the request", flush=True)
            result.extra["requested_after_seconds"] = after
            results.append(result)
    return results


def step_snapshot(world: World, all_files: list[Path]) -> dict[str, Any]:
    """116,396 rows of a device of their own, with the disk's real paths."""
    rows = all_files[:ARCHIVE_FILES]
    identity = VolumeIdentity(
        value="\\\\?\\Volume{045045045-0000-0000-0000-000000000045}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )
    from app.domain.services.device_identity import compute_device_id

    device = Device(
        id=compute_device_id(identity),
        volume_identity=identity,
        label="SNAPSHOT",
        first_seen_at=datetime.datetime.now(tz=datetime.UTC),
        last_seen_at=datetime.datetime.now(tz=datetime.UTC),
    )
    with Session(bind=world.engine) as session:
        PostgresDeviceRepository(session).save(device)
        session.commit()

    print(f"\nsnapshot: writing {len(rows)} rows for a device of their own", flush=True)
    started = time.perf_counter()
    stamp = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
    with psycopg.connect(world.connection.conninfo(), autocommit=True) as db:
        db.execute(
            "CREATE TEMP TABLE staging (id uuid, relative_path text, filename text, "
            "extension text, file_size bigint, file_modified_at timestamptz, content_hash text)"
        )
        with db.cursor().copy("COPY staging FROM STDIN") as copy:
            for index, path in enumerate(rows):
                relative = ImagePath(path.relative_to(world.mount))
                copy.write_row(
                    (
                        compute_image_id(device.id, relative).value,
                        str(relative),
                        path.stem,
                        path.suffix.lower().lstrip("."),
                        3_800_000 + index,
                        stamp + datetime.timedelta(seconds=index),
                        f"{index:064x}",
                    )
                )
        # The HNSW index is dropped in this throwaway cluster: building it for
        # 116,396 random vectors is minutes, and the snapshot never reads it.
        db.execute("DROP INDEX IF EXISTS ix_images_embedding_hnsw")
        db.execute(
            "INSERT INTO images (id, device_id, relative_path, filename, extension, "
            "file_size, file_modified_at, content_hash, embedding, capture_source, "
            "position_source) SELECT s.id, %s, s.relative_path, s.filename, s.extension, "
            "s.file_size, s.file_modified_at, s.content_hash, "
            "(SELECT array_agg(random()::real) FROM generate_series(1, 512) "
            " WHERE s.id IS NOT NULL)::vector(512), 'unknown', 'unknown' FROM staging s",
            (device.id.value,),
        )
        db.execute("VACUUM ANALYZE images")
        (table_mb,) = db.execute(
            "SELECT pg_total_relation_size('images') / 1e6"
        ).fetchone()  # type: ignore[misc]
    print(
        f"  written in {time.perf_counter() - started:.0f} s; images is {table_mb:.0f} MB",
        flush=True,
    )

    # The first read after a restart of the server: its buffers are cold.
    world.job_session.close()
    world.image_session.close()
    world.engine.dispose()
    world.cluster.stop()
    world.connection = world.cluster.start().connection
    world.engine = production_engine(world.connection)
    world.job_session = Session(bind=world.engine)
    world.image_session = Session(bind=world.engine)
    world.jobs = PostgresIndexingJobRepository(world.job_session)

    timings = []
    for attempt in range(6):
        with Session(bind=world.engine) as session:
            repository = PostgresImageRepository(session)
            started = time.perf_counter()
            signals = repository.list_change_signals(device.id)
            timings.append(time.perf_counter() - started)
            assert len(signals) == len(rows)
        del signals
        gc.collect()

    with Session(bind=world.engine) as session:
        repository = PostgresImageRepository(session)
        gc.collect()
        tracemalloc.start()
        before, _ = tracemalloc.get_traced_memory()
        signals = repository.list_change_signals(device.id)
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rows_returned = len(signals)
        del signals
    result = {
        "rows": rows_returned,
        "table_mb": table_mb,
        "first_after_restart_seconds": timings[0],
        "warm_seconds_median": statistics.median(timings[1:]),
        "all_seconds": timings,
        "retained_mb": (retained - before) / MB,
        "peak_mb": (peak - before) / MB,
    }
    print(
        f"  list_change_signals: {rows_returned} rows, first after restart "
        f"{timings[0]:.2f} s, warm median {result['warm_seconds_median']:.2f} s; "
        f"retained {result['retained_mb']:.1f} MB, peak {result['peak_mb']:.1f} MB "
        "(tracemalloc)",
        flush=True,
    )
    return result


def image_at(path: Path) -> Image:
    image_path = ImagePath(str(path))
    return Image(
        id=compute_image_id(DeviceId(uuid.UUID(int=0)), ImagePath(path.name)),
        device_id=DeviceId(uuid.UUID(int=0)),
        relative_path=ImagePath(path.name),
        absolute_path=image_path,
        filename=path.stem,
        extension=path.suffix.lstrip(".").lower(),
    )


def step_vectors(pool: list[Path], count: int) -> dict[str, Any]:
    from app.presentation.dependencies import get_embedding_model

    model = get_embedding_model()
    paths = [pool.pop() for _ in range(count)]
    images = [image_at(path) for path in paths]
    blobs = [path.read_bytes() for path in paths]
    print(
        f"\nvectors of {count} photos of the disk: bytes against path, draft against today",
        flush=True,
    )

    def batched(contents: list[bytes | None] | None) -> np.ndarray:
        vectors = []
        for start in range(0, len(images), 8):
            chunk = images[start : start + 8]
            given = contents[start : start + 8] if contents is not None else None
            vectors.extend(v.values for v in model.encode_images(chunk, given))
        return np.asarray(vectors, dtype=np.float64)

    from_path = batched(None)
    from_bytes = batched(list(blobs))
    reduced = 0
    for data in blobs:
        from io import BytesIO

        from PIL import Image as PILImage

        with PILImage.open(BytesIO(data)) as opened:
            if isinstance(opened, JpegImageFile) and min(opened.size) >= 2000:
                reduced += 1
    original = JpegImageFile.draft
    JpegImageFile.draft = lambda self, mode, size: None  # type: ignore[assignment,method-assign]
    try:
        today = batched(list(blobs))
    finally:
        JpegImageFile.draft = original  # type: ignore[method-assign]
    cosines = np.sum(from_bytes * today, axis=1)
    identical = np.all(from_bytes == today, axis=1)
    untouched = [i for i in range(count) if identical[i]]
    moved = [float(cosines[i]) for i in range(count) if not identical[i]]
    result = {
        "photos": count,
        "reduced_by_the_draft": reduced,
        "bytes_vs_path_max_abs_difference": float(np.abs(from_bytes - from_path).max()),
        "bytes_vs_path_identical": bool(np.array_equal(from_bytes, from_path)),
        "cosine_with_today_mean": float(cosines.mean()),
        "cosine_with_today_min": float(cosines.min()),
        "identical_to_today": len(untouched),
        "cosine_with_today_mean_of_the_changed": (
            float(np.mean(moved)) if moved else math.nan
        ),
        "cosine_with_today_min_of_the_changed": (
            float(np.min(moved)) if moved else math.nan
        ),
    }
    print(
        f"  bytes vs path: max |diff| {result['bytes_vs_path_max_abs_difference']:.3g} "
        f"(identical: {result['bytes_vs_path_identical']}); reduced by the draft "
        f"{reduced}/{count}; cosine with today mean {result['cosine_with_today_mean']:.4f}, "
        f"min {result['cosine_with_today_min']:.4f}",
        flush=True,
    )
    return result


def step_demo() -> dict[str, Any]:
    from tests.dataset.test_semantic_search_e2e import (
        QueryOutcome,
        hard_negative_contamination_at_k,
        pairwise_accuracy,
        recall_at_k,
        strict_top_1,
    )
    from tests.infrastructure.ai.test_reference_embeddings_slow import (
        REFERENCE,
        this_machine,
    )
    from tests.infrastructure.ai.test_reference_embeddings_slow import (
        image as reference_image,
    )

    from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
    from app.presentation.dependencies import get_embedding_model

    model = get_embedding_model()
    assert isinstance(model, ClipEmbeddingModel)
    demo = REPOSITORY_ROOT / "backend" / "dataset" / "demo"
    queries = json.loads((demo / "queries.json").read_text(encoding="utf-8"))["queries"]
    files = sorted(p for p in (demo / "images").rglob("*") if p.is_file())
    relative = [p.relative_to(demo).as_posix() for p in files]
    images = [image_at(p) for p in files]
    print(f"\ndemo corpus: {len(files)} images, {len(queries)} queries", flush=True)
    vectors = np.asarray(
        [
            v.values
            for s in range(0, len(images), 8)
            for v in model.encode_images(images[s : s + 8])
        ]
    )
    texts = np.asarray([model.encode_text(q["query"]).values for q in queries])
    outcomes = []
    for query, row in zip(queries, texts @ vectors.T, strict=True):
        order = np.argsort(-row, kind="stable")
        outcomes.append(
            QueryOutcome(
                query=query["query"],
                relevant=frozenset(query["relevant"]),
                hard_negatives=frozenset(query["hard_negatives"]),
                ranked_paths=tuple(relative[i] for i in order),
                similarities=tuple(float(row[i]) for i in order),
            )
        )
    numbers = {
        "strict_top_1": 100 * strict_top_1(outcomes),
        "recall_at_5": 100 * recall_at_k(outcomes, 5),
        "contamination_at_5": 100 * hard_negative_contamination_at_k(outcomes, 5),
        "pairwise": 100 * pairwise_accuracy(outcomes),
    }

    reference = np.load(REFERENCE)
    recorded = json.loads(str(reference["machine"]))
    found = np.asarray(
        [
            model.encode_image(reference_image(str(p))).values
            for p in reference["image_paths"]
        ],
        dtype=np.float32,
    )
    expected = reference["image_vectors"]
    batched = np.asarray(
        [
            v.values
            for s in range(0, len(reference["image_paths"]), 8)
            for v in model.encode_images(
                [reference_image(str(p)) for p in reference["image_paths"][s : s + 8]]
            )
        ],
        dtype=np.float32,
    )
    original = JpegImageFile.draft
    JpegImageFile.draft = lambda self, mode, size: None  # type: ignore[assignment,method-assign]
    try:
        without_draft = np.asarray(
            [
                v.values
                for s in range(0, len(images), 8)
                for v in model.encode_images(images[s : s + 8])
            ]
        )
        reference_without_draft = np.asarray(
            [
                model.encode_image(reference_image(str(p))).values
                for p in reference["image_paths"]
            ],
            dtype=np.float32,
        )
    finally:
        JpegImageFile.draft = original  # type: ignore[method-assign]
    result = {
        **numbers,
        "demo_vectors_identical_with_and_without_the_draft": bool(
            np.array_equal(vectors, without_draft)
        ),
        "reference_max_abs_difference_without_the_draft": float(
            np.abs(reference_without_draft - expected).max()
        ),
        "reference_machine_is_this_one": all(
            recorded.get(key) == value for key, value in this_machine().items()
        ),
        "reference_one_by_one_bit_identical": bool(
            all(
                found[i].tobytes() == expected[i].tobytes()
                for i in range(len(expected))
            )
        ),
        "reference_one_by_one_max_abs_difference": float(
            np.abs(found - expected).max()
        ),
        "reference_batched_max_abs_difference": float(
            np.abs(batched - reference["image_vectors_batched"]).max()
        ),
    }
    print(
        "  strict top-1 {strict_top_1:.1f}  Recall@5 {recall_at_5:.1f}  "
        "contamination@5 {contamination_at_5:.1f}  pairwise {pairwise:.1f}".format(
            **numbers
        )
        + f"; reference vectors: max |diff| {result['reference_one_by_one_max_abs_difference']:.3g}"
        f" one by one, {result['reference_batched_max_abs_difference']:.3g} batched "
        f"(recorded on this machine: {result['reference_machine_is_this_one']}; without "
        f"the draft {result['reference_max_abs_difference_without_the_draft']:.3g}); demo "
        "vectors identical with and without the draft: "
        f"{result['demo_vectors_identical_with_and_without_the_draft']}",
        flush=True,
    )
    return result


def step_unplug(world: World, pool: list[Path], clock: ClockSampler) -> JobResult:
    group = [pool.pop() for _ in range(2000)]
    print(
        "\nunplug: a job of 2,000 cold photos is starting; UNPLUG THE DISK when "
        "asked, and the time from its disappearance to the job's end is measured",
        flush=True,
    )
    gone: dict[str, float] = {}

    def watch(job: IndexingJob) -> None:
        def poll() -> None:
            time.sleep(30)
            print("  >>> unplug the disk now", flush=True)
            while world.root.exists():
                time.sleep(0.05)
            gone["at"] = time.perf_counter()

        threading.Thread(target=poll, daemon=True).start()

    result = run_job(world, "unplug", 32, group, clock, during=watch)
    result.extra["unplug_to_end_seconds"] = (
        result.extra["finished_at"] - gone["at"] if "at" in gone else None
    )
    print(
        f"  unplugged -> {result.status} in {result.extra['unplug_to_end_seconds']} s",
        flush=True,
    )
    return result


def step_restart() -> dict[str, Any]:
    """Section 10, "O recomeço": the development database, migrated for real."""
    from tests.dataset.test_semantic_search_e2e import (
        QueryOutcome,
        hard_negative_contamination_at_k,
        pairwise_accuracy,
        recall_at_k,
        strict_top_1,
    )

    from app.application.use_cases.search_images import SearchImagesUseCase
    from app.infrastructure.persistence.session import SessionLocal
    from app.infrastructure.workers.indexing_worker import run as index_with_the_cli
    from app.presentation.dependencies import get_embedding_model

    # The real scan, and the real class: this step runs the CLI as it ships.
    job_runner_module.FilesystemImageProvider = _ORIGINAL_PROVIDER  # type: ignore[misc]
    job_runner_module.ReadAheadCandidates = _ORIGINAL_READ_AHEAD  # type: ignore[misc]
    settings.indexing_read_ahead = True  # type: ignore[misc]
    settings.read_ahead_files = 32  # type: ignore[misc]

    runtime = PostgresRuntime(settings.embedded_postgres_runtime_directory)
    cluster = EmbeddedPostgres(
        runtime, ClusterHome(settings.embedded_postgres_directory)
    )
    connection = cluster.start().connection

    def census() -> dict[str, Any]:
        with psycopg.connect(connection.conninfo(), autocommit=True) as db:
            counts = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # type: ignore[index]
                for table in (
                    "images",
                    "indexing_jobs",
                    "indexing_job_scopes",
                    "devices",
                )
            }
            revision = db.execute("SELECT version_num FROM alembic_version").fetchone()
            devices = db.execute(
                "SELECT label, id::text, volume_identity FROM devices ORDER BY id"
            ).fetchall()
        return {
            "revision": revision[0] if revision else None,
            **counts,
            "devices": [list(row) for row in devices],
        }

    before = census()
    print(f"\nrestart: the development database before: {before}", flush=True)
    started = time.perf_counter()
    cluster.prepare(connection)
    migrated = time.perf_counter() - started
    after = census()
    print(f"  after prepare() ({migrated:.1f} s): {after}", flush=True)

    demo = (REPOSITORY_ROOT / "backend" / "dataset" / "demo").resolve()
    started = time.perf_counter()
    index_with_the_cli(demo, "")
    indexed_in = time.perf_counter() - started

    queries = json.loads((demo / "queries.json").read_text(encoding="utf-8"))["queries"]
    session = SessionLocal()
    try:
        search = SearchImagesUseCase(
            repository=PostgresImageRepository(session),
            embedding_model=get_embedding_model(),
            default_limit=5,
        )
        outcomes = []
        for query in queries:
            hits = search.execute(query["query"], limit=45)
            ranked = tuple(
                str(hit.image.relative_path).split("dataset/demo/", 1)[-1]
                for hit in hits
            )
            outcomes.append(
                QueryOutcome(
                    query=query["query"],
                    relevant=frozenset(query["relevant"]),
                    hard_negatives=frozenset(query["hard_negatives"]),
                    ranked_paths=ranked,
                    similarities=tuple(hit.similarity for hit in hits),
                )
            )
    finally:
        session.close()
    reindexed = census()
    numbers = {
        "strict_top_1": 100 * strict_top_1(outcomes),
        "recall_at_5": 100 * recall_at_k(outcomes, 5),
        "contamination_at_5": 100 * hard_negative_contamination_at_k(outcomes, 5),
        "pairwise": 100 * pairwise_accuracy(outcomes),
    }
    print(f"  demo reindexed by the CLI in {indexed_in:.1f} s: {reindexed}", flush=True)
    print(
        "  searched in the development database: strict top-1 {strict_top_1:.1f}  "
        "Recall@5 {recall_at_5:.1f}  contamination@5 {contamination_at_5:.1f}  "
        "pairwise {pairwise:.1f}".format(**numbers),
        flush=True,
    )
    return {
        "before": before,
        "migration_seconds": migrated,
        "after": after,
        "demo_indexed_seconds": indexed_in,
        "reindexed": reindexed,
        **numbers,
    }


# ---------------------------------------------------------------------------


def list_supported(root: Path) -> list[Path]:
    """Every supported file, by directory listing only, as the pilots listed."""
    extensions = {ext.lower() for ext in settings.supported_extensions}
    found: list[Path] = []
    for directory, _, filenames in os.walk(root, onerror=lambda error: None):
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in extensions:
                found.append(Path(directory) / filename)
    return found


def pooled(results: list[JobResult], mode: str) -> dict[str, float]:
    arms = [r for r in results if r.mode == mode]
    steady = [r.steady_seconds_per_image for r in arms]
    return {
        "arms": len(arms),
        "steady_seconds_per_image": statistics.fmean(steady),
        "steady_min": min(steady),
        "steady_max": max(steady),
        "seconds_per_image": statistics.fmean(r.seconds_per_image for r in arms),
        "hours_for_the_archive": statistics.fmean(steady) * ARCHIVE_FILES / 3600,
        "peak_working_set_mb": max(r.peak_working_set_mb for r in arms),
        "peak_private_mb": max(r.peak_private_mb for r in arms),
        "read_ahead_peak_mb": max(r.read_ahead_peak_mb for r in arms),
        "wait_ms_per_image": 1000
        * statistics.fmean(r.candidate_wait_seconds / r.files for r in arms),
        "metadata_share_percent": 100
        * statistics.fmean(r.metadata_seconds / r.seconds for r in arms),
        "metadata_ms_per_query": 1000
        * sum(r.metadata_seconds for r in arms)
        / max(1, sum(r.metadata_queries for r in arms)),
        "inference_ms": 1000
        * statistics.fmean(r.inference_seconds / max(1, r.indexed) for r in arms),
        "thumbnail_ms": 1000
        * statistics.fmean(r.thumbnail_seconds / max(1, r.indexed) for r in arms),
        "mhz": statistics.fmean([r.mhz for r in arms if r.mhz] or [0.0]),
    }


def main() -> None:
    global GROUP, SEED
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--steps",
        default="first,rescan,touched,cancel,snapshot,vectors,demo",
        help="Comma-separated; `unplug` is never in the default.",
    )
    parser.add_argument("--cycles", type=int, default=4)
    parser.add_argument("--group", type=int, default=GROUP)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--rescan", type=int, default=2000)
    parser.add_argument(
        "--cancel-after",
        default="10,75",
        help="Seconds into the job, comma-separated: one during the serial scan "
        "of its window, one during its batches.",
    )
    parser.add_argument("--cancel-repetitions", type=int, default=2)
    parser.add_argument(
        "--json",
        type=Path,
        default=Path(__file__).with_name("measure_read_ahead.json"),
    )
    args = parser.parse_args()
    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    GROUP, SEED = args.group, args.seed

    print(f"RFC-045 measurement, {datetime.datetime.now():%Y-%m-%d %H:%M}, seed {SEED}")
    print(
        f"machine: {platform.platform()}, {platform.processor()}, {os.cpu_count()} threads"
    )
    print(
        f"settings: batch {settings.batch_size}, metadata_prefetch_size "
        f"{settings.metadata_prefetch_size}, read_ahead_bytes {settings.read_ahead_bytes}, "
        f"read_ahead_max_file_bytes {settings.read_ahead_max_file_bytes}, thumbnails "
        f"{settings.thumbnail_max_edge} px",
        flush=True,
    )

    JobProgressObserver.window_decided = _recording_window_decided  # type: ignore[method-assign]
    job_runner_module.FilesystemImageProvider = SampledProvider  # type: ignore[misc,assignment]
    job_runner_module.ReadAheadCandidates = RecordedReadAhead  # type: ignore[misc]

    report: dict[str, Any] = {}
    if steps == ["restart"]:
        report["restart"] = step_restart()
        args.json.with_name(args.json.stem + "_restart.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        return

    started = time.perf_counter()
    all_files = list_supported(args.root)
    print(
        f"listing of {args.root}: {len(all_files)} supported files in "
        f"{time.perf_counter() - started:.0f} s",
        flush=True,
    )
    pool = list(all_files)
    random.Random(SEED).shuffle(pool)

    report = {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "seed": SEED,
        "machine": {"platform": platform.platform(), "processor": platform.processor()},
        "files_on_disk": len(all_files),
        "steps": steps,
    }
    scratch = Path(tempfile.mkdtemp(prefix="rfc045-"))
    world: World | None = None
    try:
        from app.presentation.dependencies import get_embedding_model

        loaded = time.perf_counter()
        get_embedding_model().warm_up()
        print(f"model loaded in {time.perf_counter() - loaded:.1f} s", flush=True)
        needs_world = {"first", "rescan", "touched", "cancel", "snapshot", "unplug"}
        if needs_world & set(steps):
            world = World(args.root, scratch)

        with ClockSampler(scratch / "clock.csv") as clock:
            if "first" in steps:
                assert world is not None
                first = step_first(world, pool, clock, args.cycles)
                report["first"] = [asdict(r) for r in first]
                report["first_pooled"] = {
                    mode: pooled(first, mode) for mode in ("off", "on/32", "on/64")
                }
                off = report["first_pooled"]["off"]["steady_seconds_per_image"]
                for mode in ("on/32", "on/64"):
                    on = report["first_pooled"][mode]["steady_seconds_per_image"]
                    report["first_pooled"][mode]["faster_than_off_percent"] = 100 * (
                        1 - on / off
                    )
                print("\nfirst indexing, pooled:", flush=True)
                for mode, numbers in report["first_pooled"].items():
                    print(
                        f"  {mode:<6} steady {numbers['steady_seconds_per_image']:.3f} s/img "
                        f"({numbers['steady_min']:.3f}-{numbers['steady_max']:.3f}), "
                        f"{numbers['hours_for_the_archive']:.1f} h for {ARCHIVE_FILES}; "
                        f"faster than off: {numbers.get('faster_than_off_percent', 0):.1f}%; "
                        f"WS peak {numbers['peak_working_set_mb']:.0f} MB, private "
                        f"{numbers['peak_private_mb']:.0f} MB, held {numbers['read_ahead_peak_mb']:.0f} MB; "
                        f"wait {numbers['wait_ms_per_image']:.0f} ms/img; metadata "
                        f"{numbers['metadata_share_percent']:.2f}% of the job "
                        f"({numbers['metadata_ms_per_query']:.1f} ms/query); "
                        f"infer {numbers['inference_ms']:.0f} thumb {numbers['thumbnail_ms']:.0f}; "
                        f"{numbers['mhz']:.0f} MHz",
                        flush=True,
                    )
            chosen: list[Path] = []
            if "rescan" in steps:
                assert world is not None
                rescans, chosen = step_rescan(world, clock, args.rescan)
                report["rescan"] = [asdict(r) for r in rescans]
                timed = [r for r in rescans if r.step == "rescan"]
                for mode in ("off", "on/32"):
                    seconds = [r.seconds for r in timed if r.mode == mode]
                    report.setdefault("rescan_pooled", {})[mode] = {
                        "seconds_mean": statistics.fmean(seconds),
                        "seconds": seconds,
                        "contents_prefetched": [
                            r.contents_prefetched for r in timed if r.mode == mode
                        ],
                    }
                pooled_rescan = report["rescan_pooled"]
                difference = 100 * (
                    pooled_rescan["on/32"]["seconds_mean"]
                    / pooled_rescan["off"]["seconds_mean"]
                    - 1
                )
                pooled_rescan["on_minus_off_percent"] = difference
                print(
                    f"  re-scan: off {pooled_rescan['off']['seconds_mean']:.1f} s, on "
                    f"{pooled_rescan['on/32']['seconds_mean']:.1f} s ({difference:+.1f}%)",
                    flush=True,
                )
            if "touched" in steps:
                assert world is not None
                if not chosen:
                    chosen = indexed_paths(world)[: args.rescan]
                report["touched"] = asdict(step_touched(world, clock, chosen))
            if "cancel" in steps:
                assert world is not None
                delays = [float(value) for value in args.cancel_after.split(",")]
                cancels = step_cancel(
                    world, pool, clock, args.cancel_repetitions, delays
                )
                report["cancel"] = [asdict(r) for r in cancels]
            if "unplug" in steps:
                assert world is not None
                report["unplug"] = asdict(step_unplug(world, pool, clock))
            if "snapshot" in steps:
                assert world is not None
                report["snapshot"] = step_snapshot(world, all_files)
        if "vectors" in steps:
            report["vectors"] = step_vectors(pool, 50)
        if "demo" in steps:
            report["demo"] = step_demo()
    finally:
        if world is not None:
            with contextlib.suppress(Exception):
                world.close()
        shutil.rmtree(scratch, ignore_errors=True)
        args.json.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nwritten {args.json}", flush=True)


if __name__ == "__main__":
    main()
