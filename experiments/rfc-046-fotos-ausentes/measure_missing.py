"""RFC-046 section 10: the photos no longer on the disk, measured as they ship.

Every job here is run by the `JobRunner` that `build_runner()` composes -- the
real provider with `Path.walk()`, the read-ahead, the CLIP adapter, the
thumbnails, the sweep -- against an **embedded PostgreSQL 16 of its own**,
created in a temporary directory and removed at the end. The development
database is not touched. **Nothing is written to `E:`**: the trials that move
and delete photos run on a tree in `C:`.

One piece is replaced: the `DeviceLocator`. The tree is a folder of `C:`, not
a volume, so a device whose "mount point" is the tree's root stands for a
disk; a job of the whole disk is then a job of the whole tree (RFC-039 section
2.9 registered `C:` itself, whose whole disk is not a thing to index).

Steps, each a row of section 10:

    tree      the trials on `C:`: index a tree of 270 photos; move a folder of
              200 and index (200 marked, 200 new, a search finds one photo);
              move it back and index (200 unmarked, 200 marked, no inference);
              deny a folder of 50 with `icacls` and index (0 marked, a
              WARNING); delete 20 as the Explorer does on a volume and index
              (20 marked, no row in `$RECYCLE.BIN`); purge the 220 through the
              route (220 rows and 220 thumbnails fewer, the card without them)
    search    the search's p95 on 100,000 rows seeded as `measure_map.py
              --seed-rows` (RFC-040), 1% marked, with and without the filter
    sweep     the sweep of a synthetic device of 116,396 rows, 1% missing:
              seconds and MB. The paths of `--root` when given; otherwise
              synthetic paths shaped like a drone's
    listing   the listing of `--root`, `walk()` against `rglob()`, alternated:
              equal entry by entry, and the times
    rescan    a new scan of `--root` with no change, cold: the listing plus
              the per-file cost (`stat` and the EXIF header) of a sample of
              cold files, projected to the whole disk
    unplug    never by default: a job over a pen drive (`--pen`) that waits
              for it to be pulled out by hand, and says how the job ended

Run it from the repository root:

    .venv/Scripts/python.exe experiments/rfc-046-fotos-ausentes/measure_missing.py \\
        --steps tree,search,sweep > measure_missing.log
    .venv/Scripts/python.exe experiments/rfc-046-fotos-ausentes/measure_missing.py \\
        --root E:/ --steps listing,sweep,rescan >> ...
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import gc
import json
import logging
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import uuid
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
sys.path.insert(0, str(REPOSITORY_ROOT))

import psycopg  # noqa: E402
from PIL import Image as PillowImage  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.application.use_cases.sweep_missing_images import (  # noqa: E402
    SweepMissingImagesUseCase,
)
from app.domain.entities.device import Device  # noqa: E402
from app.domain.entities.indexing_job import IndexingJob  # noqa: E402
from app.domain.services.device_identity import compute_device_id  # noqa: E402
from app.domain.services.device_locator import DeviceLocator, FolderEntry  # noqa: E402
from app.domain.value_objects.device_id import (  # noqa: E402
    VolumeIdentity,
    VolumeKind,
)
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.domain.value_objects.job_id import JobId  # noqa: E402
from app.domain.value_objects.job_scope import JobScope  # noqa: E402
from app.infrastructure.config.settings import settings  # noqa: E402
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres  # noqa: E402
from app.infrastructure.database.embedded.cluster_home import ClusterHome  # noqa: E402
from app.infrastructure.database.embedded.runtime import PostgresRuntime  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.filesystem.exif_capture_date import (  # noqa: E402
    read_exif_facts,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    _list_tree,
    discovery_sort_key,
)
from app.infrastructure.filesystem.image_identity import compute_image_id  # noqa: E402
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
from app.infrastructure.workers.indexing_worker import listed_identity  # noqa: E402

SEED = 20261031
ARCHIVE_FILES = 116_396
MB = 1024 * 1024
DEMO = REPOSITORY_ROOT / "backend" / "dataset" / "demo" / "images"
MAP_SCRIPT = REPOSITORY_ROOT / "experiments" / "rfc-040-map" / "measure_map.py"
SUPPORTED = {ext.lower() for ext in settings.supported_extensions}

MOVED, DENIED, DELETED = 200, 50, 20
MOVED_FOLDER = "voo-2026-03-12"
DENIED_FOLDER = "bloqueada"
DELETED_FOLDER = "soltas"


# ---------------------------------------------------------------------------
# Memory of this process, as the RFC-045 measurement read it
# ---------------------------------------------------------------------------


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


def private_bytes() -> int:
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not _KERNEL32.K32GetProcessMemoryInfo(
        _KERNEL32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return int(counters.PrivateUsage)


class PeakPrivate:
    """The peak private bytes over a block, sampled every 10 ms."""

    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> PeakPrivate:
        self.before = private_bytes()
        self.peak = self.before
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, private_bytes())

    def _run(self) -> None:
        while not self._stop.wait(0.01):
            self.peak = max(self.peak, private_bytes())


# ---------------------------------------------------------------------------
# A cluster of its own
# ---------------------------------------------------------------------------


class Cluster:
    def __init__(self, scratch: Path) -> None:
        runtime = PostgresRuntime(settings.embedded_postgres_runtime_directory)
        self.cluster = EmbeddedPostgres(
            runtime, ClusterHome(scratch / "SolidVision" / "postgres")
        )
        started = time.perf_counter()
        self.connection = self.cluster.start().connection
        self.cluster.prepare(self.connection)
        self.engine = create_engine(self.connection.url(), pool_pre_ping=True)
        version = self.scalar("SHOW server_version")
        revision = self.scalar("SELECT version_num FROM alembic_version")
        print(
            f"embedded PostgreSQL {version} ready in "
            f"{time.perf_counter() - started:.1f} s, at revision {revision}",
            flush=True,
        )

    def scalar(self, sql: str) -> Any:
        with self.engine.connect() as db:
            return db.execute(text(sql)).scalar_one()

    def restart(self) -> None:
        self.engine.dispose()
        self.cluster.stop()
        self.connection = self.cluster.start().connection
        self.engine = create_engine(self.connection.url(), pool_pre_ping=True)

    def close(self) -> None:
        self.engine.dispose()
        self.cluster.stop()


def synthetic_device(label: str, tag: int) -> Device:
    identity = VolumeIdentity(
        value=f"\\\\?\\Volume{{04604604{tag}-0000-0000-0000-000000000046}}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )
    now = datetime.datetime.now(tz=datetime.UTC)
    return Device(
        id=compute_device_id(identity),
        volume_identity=identity,
        label=label,
        first_seen_at=now,
        last_seen_at=now,
    )


class TreeLocator(DeviceLocator):
    """A device whose disk is a folder: `root` is its mount point."""

    def __init__(self, device: Device, root: Path) -> None:
        self._device = device
        self._root = root

    def mount_point(self, device: Device) -> Path | None:
        return self._root if device.id == self._device.id else None

    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        target = self._root / str(scope)
        return scope if target.is_dir() else None

    def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
        return None


# ---------------------------------------------------------------------------
# tree: the trials on C:
# ---------------------------------------------------------------------------


def demo_photos() -> list[Path]:
    return sorted(
        path
        for path in DEMO.rglob("*")
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )


def make_photos(folder: Path, count: int, generator: random.Random, start: int) -> None:
    """`count` different photos: crops of the demo's, at random places and scales.

    Each is its own picture -- no two share their bytes or their crop -- so a
    search with the vector of one finds it and not a sibling.
    """
    folder.mkdir(parents=True, exist_ok=True)
    sources = demo_photos()
    for index in range(count):
        with PillowImage.open(generator.choice(sources)) as opened:
            picture = opened.convert("RGB")
        width, height = picture.size
        scale = generator.uniform(0.45, 0.9)
        crop_w, crop_h = int(width * scale), int(height * scale)
        left = generator.randint(0, width - crop_w)
        top = generator.randint(0, height - crop_h)
        crop = picture.crop((left, top, left + crop_w, top + crop_h))
        if generator.random() < 0.5:
            crop = crop.transpose(PillowImage.Transpose.FLIP_LEFT_RIGHT)
        crop.save(folder / f"DJI_{start + index:04d}.JPG", quality=90)


class TreeWorld:
    def __init__(self, scratch: Path, tree: Path) -> None:
        self.scratch = scratch
        self.tree = tree
        self.cluster = Cluster(scratch)
        self.thumbnails = scratch / "thumbnails"
        settings.thumbnail_directory = self.thumbnails  # type: ignore[misc]
        self.job_session = Session(bind=self.cluster.engine)
        self.image_session = Session(bind=self.cluster.engine)
        self.device = synthetic_device("ARVORE", 6)
        PostgresDeviceRepository(self.job_session).save(self.device)
        self.jobs = PostgresIndexingJobRepository(self.job_session)
        self.runner = job_runner_module.build_runner(
            self.job_session, self.image_session
        )
        self.runner._locator = TreeLocator(self.device, tree)  # type: ignore[attr-defined]
        started = time.perf_counter()
        self.runner.warm_up_now()
        print(f"model loaded in {time.perf_counter() - started:.1f} s", flush=True)

    def index(self) -> dict[str, Any]:
        job = self.jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=self.device.id,
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )
        before = self.presence()
        records = LogRecords()
        started = time.perf_counter()
        with records:
            finished = self.runner.run_job(job.id)
        seconds = time.perf_counter() - started
        assert finished is not None
        after = self.presence()
        newly_marked = {p for p, m in after.items() if m and not before.get(p, False)}
        unmarked = {p for p, m in after.items() if not m and before.get(p, False)}
        created = set(after) - set(before)
        return {
            "status": finished.status.value,
            "seconds": round(seconds, 1),
            "inferred": finished.progress.processed_images,
            "skipped": finished.progress.skipped_images,
            "failed": finished.progress.failed_images,
            "rows": len(after),
            "visible": sum(1 for marked in after.values() if not marked),
            "newly_marked": len(newly_marked),
            "unmarked": len(unmarked),
            "created": len(created),
            "marked_paths": sorted(newly_marked),
            "warnings": records.warnings(),
            "sweep_lines": records.matching("marked as not found"),
        }

    def presence(self) -> dict[str, bool]:
        with Session(bind=self.cluster.engine) as session:
            rows = PostgresImageRepository(session).list_presence(self.device.id)
        return {row.relative_path: row.missing for row in rows}

    def thumbnail_files(self) -> int:
        return sum(1 for path in self.thumbnails.rglob("*.jpg") if path.is_file())

    def close(self) -> None:
        self.job_session.close()
        self.image_session.close()
        self.cluster.close()


class LogRecords(logging.Handler):
    """The log lines of one job, as the job's log would hold them."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self) -> LogRecords:
        logging.getLogger("app").addHandler(self)
        return self

    def __exit__(self, *exc_info: object) -> None:
        logging.getLogger("app").removeHandler(self)

    def warnings(self) -> list[str]:
        return [
            r.getMessage()
            for r in self.records
            if r.levelno == logging.WARNING
            and (
                "could not be read" in r.getMessage()
                or "Could not read" in r.getMessage()
            )
        ]

    def matching(self, fragment: str) -> list[str]:
        return [r.getMessage() for r in self.records if fragment in r.getMessage()]


def emulate_explorer_delete(path: Path, root: Path) -> Path:
    """What the Explorer does to a file on a volume: into its root's bin.

    `$RECYCLE.BIN\\<SID>\\$R<random><ext>` with the bytes, and `$I<random><ext>`
    beside it with the index -- the extension of the deleted file on both,
    which is why the `$I` files used to fail as photos (RFC-045 section
    14.10). Emulated because the tree is not a volume: on `C:` the real
    Explorer would send the file to `C:\\$Recycle.Bin`, outside the tree.
    """
    bin_folder = (
        root / "$RECYCLE.BIN" / "S-1-5-21-0000000046-0000000046-0000000046-1001"
    )
    bin_folder.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6].upper()
    target = bin_folder / f"$R{tag}{path.suffix}"
    shutil.move(path, target)
    (bin_folder / f"$I{tag}{path.suffix}").write_bytes(b"\x02" + b"\x00" * 543)
    return target


def step_tree(scratch: Path) -> dict[str, Any]:
    tree = scratch / "arvore"
    generator = random.Random(SEED)
    make_photos(tree / MOVED_FOLDER, MOVED, generator, 1)
    make_photos(tree / DENIED_FOLDER, DENIED, generator, 1001)
    make_photos(tree / DELETED_FOLDER, DELETED, generator, 2001)
    print(f"\ntree: {MOVED + DENIED + DELETED} photos written under {tree}", flush=True)
    world = TreeWorld(scratch, tree)
    result: dict[str, Any] = {}
    try:
        result["first"] = world.index()
        report("first indexing", result["first"])

        (tree / MOVED_FOLDER).rename(tree / f"{MOVED_FOLDER}-movida")
        result["moved"] = world.index()
        result["moved"]["search"] = search_one_moved(world)
        report("folder moved", result["moved"])

        (tree / f"{MOVED_FOLDER}-movida").rename(tree / MOVED_FOLDER)
        result["moved_back"] = world.index()
        report("folder moved back", result["moved_back"])

        user = os.environ.get("USERNAME", "")
        denied = subprocess.run(
            ["icacls", str(tree / DENIED_FOLDER), "/deny", f"{user}:(RD)"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            result["denied"] = world.index()
            result["denied"]["icacls"] = denied.returncode
        finally:
            subprocess.run(
                ["icacls", str(tree / DENIED_FOLDER), "/remove:d", user],
                capture_output=True,
                check=False,
            )
        report("folder denied", result["denied"])

        deleted = sorted((tree / DELETED_FOLDER).iterdir())[:DELETED]
        for path in deleted:
            emulate_explorer_delete(path, tree)
        result["deleted"] = world.index()
        result["deleted"]["rows_in_the_bin"] = sum(
            1 for path in world.presence() if path.upper().startswith("$RECYCLE.BIN")
        )
        report("deleted by the Explorer", result["deleted"])

        result["purge"] = purge_through_the_route(world)
        print(f"  purge: {json.dumps(result['purge'])}", flush=True)
    finally:
        world.close()
    return result


def search_one_moved(world: TreeWorld) -> dict[str, Any]:
    """`Parecidas com esta` on a moved photo: how many copies of it come back."""
    moved = f"{MOVED_FOLDER}-movida/DJI_0001.JPG"
    original = f"{MOVED_FOLDER}/DJI_0001.JPG"
    with Session(bind=world.cluster.engine) as session:
        repository = PostgresImageRepository(session)
        image_id = compute_image_id(world.device.id, ImagePath(moved))
        vector = repository.get_embedding(image_id)
        assert vector is not None
        hits = repository.search_similar(vector, 10)
        old_id = compute_image_id(world.device.id, ImagePath(original))
        old_still_there = repository.get(old_id) is not None
    identical = [hit for hit in hits if hit.similarity > 0.9999]
    return {
        "hits_identical": len(identical),
        "paths": [str(hit.image.relative_path) for hit in identical],
        "next_similarity": (
            round(hits[len(identical)].similarity, 4)
            if len(hits) > len(identical)
            else None
        ),
        "old_row_still_in_the_database": old_still_there,
    }


def purge_through_the_route(world: TreeWorld) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
    from app.infrastructure.persistence.session import get_db
    from app.presentation.api import app
    from app.presentation.dependencies import get_thumbnail_store

    def session() -> Iterator[Session]:
        with Session(bind=world.cluster.engine) as opened:
            yield opened

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_thumbnail_store] = lambda: FilesystemThumbnailStore(
        world.thumbnails
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            before = device_row(client, world.device)
            rows_before = sum(1 for _ in world.presence())
            thumbs_before = world.thumbnail_files()
            started = time.perf_counter()
            response = client.delete(
                f"/api/v1/devices/{world.device.id}/missing-images"
            )
            seconds = time.perf_counter() - started
            after = device_row(client, world.device)
            rows_after = sum(1 for _ in world.presence())
            thumbs_after = world.thumbnail_files()
    finally:
        app.dependency_overrides.clear()
    return {
        "status": response.status_code,
        "body": response.json(),
        "seconds": round(seconds, 3),
        "card_before": {
            k: before[k] for k in ("indexed_images", "missing_images", "missing_since")
        },
        "card_after": {
            k: after[k] for k in ("indexed_images", "missing_images", "missing_since")
        },
        "rows_fewer": rows_before - rows_after,
        "thumbnails_fewer": thumbs_before - thumbs_after,
    }


def device_row(client: Any, device: Device) -> dict[str, Any]:
    devices = client.get("/api/v1/devices").json()["devices"]
    return next(row for row in devices if row["id"] == str(device.id))


def report(name: str, outcome: dict[str, Any]) -> None:
    shown = {k: v for k, v in outcome.items() if k not in ("marked_paths",)}
    print(f"  {name}: {json.dumps(shown, ensure_ascii=False)}", flush=True)


# ---------------------------------------------------------------------------
# search: p95 with and without the filter, on 100,000 rows
# ---------------------------------------------------------------------------


def load_map_script() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("measure_map", MAP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_map"] = module
    spec.loader.exec_module(module)
    return module


def seed_map_rows(cluster: Cluster, rows: int) -> None:
    """`measure_map.py --seed-rows`'s rows, written by its own plan and SQL."""
    measure_map = load_map_script()
    _, planned = measure_map.seed_plan(rows)
    vector = "[" + ",".join(f"{v:.6f}" for v in measure_map.unit_vector(7)) + "]"
    began = time.perf_counter()
    with Session(bind=cluster.engine) as session:
        session.execute(text("DROP INDEX IF EXISTS ix_images_embedding_hnsw"))
        now = datetime.datetime.now(tz=datetime.UTC)
        session.execute(
            text(
                "INSERT INTO devices (id, volume_identity, volume_kind, label, "
                "first_seen_at, last_seen_at) VALUES (:id, :identity, "
                "'windows-volume-guid', 'HD-BENCH', :now, :now) ON CONFLICT DO NOTHING"
            ),
            {
                "id": str(measure_map.BENCH_DEVICE),
                "identity": "\\\\?\\Volume{bench-040}\\",
                "now": now,
            },
        )
        statement = text(
            "INSERT INTO images (id, device_id, relative_path, filename, extension, "
            "embedding, latitude, longitude, position_source, captured_at, "
            "capture_source) "
            "SELECT gen_random_uuid(), :device, 'bench/' || (:offset + n) || '.jpg', "
            "'bench', 'jpg', CAST(:vector AS vector), lat, lon, "
            "CASE WHEN lat IS NULL THEN 'unknown' ELSE 'exif_gps' END, captured, "
            "CASE WHEN captured IS NULL THEN 'unknown' ELSE 'exif_original' END "
            "FROM unnest(CAST(:lats AS double precision[]), "
            "CAST(:lons AS double precision[]), CAST(:dates AS timestamp[])) "
            "WITH ORDINALITY AS t(lat, lon, captured, n)"
        )
        for offset in range(0, len(planned), 10_000):
            part = planned[offset : offset + 10_000]
            session.execute(
                statement,
                {
                    "device": str(measure_map.BENCH_DEVICE),
                    "offset": offset,
                    "vector": vector,
                    "lats": [r["position"][0] if r["position"] else None for r in part],
                    "lons": [r["position"][1] if r["position"] else None for r in part],
                    "dates": [r["captured"] for r in part],
                },
            )
        session.commit()
    print(f"  seeded {rows} rows in {time.perf_counter() - began:.1f} s", flush=True)


def step_search(scratch: Path, rounds: int, queries: int) -> dict[str, Any]:
    print("\nsearch: 100,000 rows of measure_map.py, 1% marked", flush=True)
    cluster = Cluster(scratch)
    try:
        seed_map_rows(cluster, 100_000)
        with cluster.engine.begin() as db:
            marked = db.execute(
                text(
                    "UPDATE images SET missing_since = now() WHERE id IN (SELECT id "
                    "FROM images ORDER BY md5(id::text) LIMIT 1000)"
                )
            ).rowcount
            db.execute(text("ANALYZE images"))
        print(f"  {marked} rows marked", flush=True)

        generator = random.Random(SEED)
        vectors = []
        for _ in range(queries):
            values = [generator.gauss(0, 1) for _ in range(512)]
            norm = sum(v * v for v in values) ** 0.5
            vectors.append([v / norm for v in values])

        from app.domain.value_objects.embedding_vector import EmbeddingVector
        from app.infrastructure.persistence import postgres_image_repository as module

        def without_the_filter(embedding: Any, limit: int, filters: Any = None) -> Any:
            distance = ImageModel.embedding.cosine_distance(
                list(embedding.values)
            ).label("distance")
            statement = (
                select(*module._ENTITY_COLUMNS, distance)
                .where(ImageModel.embedding.is_not(None))
                .order_by(distance, ImageModel.id)
                .limit(limit)
            )
            return statement

        shipped = module.PostgresImageRepository.search_statement
        timings: dict[str, list[float]] = {"with": [], "without": []}
        plans: dict[str, str] = {}
        with Session(bind=cluster.engine) as session:
            for name, build in (("with", shipped), ("without", without_the_filter)):
                statement = build(EmbeddingVector(vectors[0]), 40)
                plan = (
                    session.execute(
                        text(
                            "EXPLAIN "
                            + str(
                                statement.compile(
                                    dialect=session.bind.dialect,  # type: ignore[union-attr]
                                    compile_kwargs={"literal_binds": True},
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                plans[name] = "\n".join(plan)
            # Warm both, then interleave them query by query, the one that
            # goes first alternating: a block of fifty of one and then fifty
            # of the other put the machine's drift between the two (the first
            # run of this step did that, and read +16.9% at the p95 and +2.9%
            # at the p50 over two identical plans).
            for build in (shipped, without_the_filter):
                for vector in vectors[:5]:
                    session.execute(build(EmbeddingVector(vector), 40)).all()
            for round_index in range(rounds):
                for index, vector in enumerate(vectors):
                    order = (
                        ("with", "without")
                        if (round_index + index) % 2 == 0
                        else ("without", "with")
                    )
                    for name in order:
                        build = shipped if name == "with" else without_the_filter
                        started = time.perf_counter()
                        rows = session.execute(build(EmbeddingVector(vector), 40)).all()
                        timings[name].append((time.perf_counter() - started) * 1000)
                        assert len(rows) == 40
                session.rollback()
        result: dict[str, Any] = {
            "marked": marked,
            "queries_each": len(timings["with"]),
        }
        for name, values in timings.items():
            ordered = sorted(values)
            result[name] = {
                "p50_ms": round(statistics.median(ordered), 2),
                "p95_ms": round(ordered[int(0.95 * len(ordered)) - 1], 2),
                "mean_ms": round(statistics.fmean(ordered), 2),
            }
        result["p95_difference_percent"] = round(
            100 * (result["with"]["p95_ms"] / result["without"]["p95_ms"] - 1), 1
        )
        result["plan_with"] = plans["with"]
        result["plan_without"] = plans["without"]
        print(
            "  "
            + json.dumps({k: v for k, v in result.items() if not k.startswith("plan")})
        )
        print("  plan with the filter:\n    " + plans["with"].replace("\n", "\n    "))
        return result
    finally:
        cluster.close()


# ---------------------------------------------------------------------------
# sweep: 116,396 rows, 1% missing
# ---------------------------------------------------------------------------


def synthetic_paths(count: int) -> list[str]:
    """Paths shaped like a drone archive: dated flights, DJI folders and names."""
    generator = random.Random(SEED)
    paths = []
    day = datetime.date(2018, 1, 1)
    while len(paths) < count:
        day += datetime.timedelta(days=generator.randint(1, 4))
        flights = generator.randint(1, 3)
        for flight in range(flights):
            site = generator.randint(1, 60)
            folder = (
                f"Drone/{day:%Y}/{day:%Y-%m-%d} Voo {flight + 1} - Obra {site:02d}"
                "/DCIM/100MEDIA"
            )
            for index in range(generator.randint(60, 400)):
                paths.append(f"{folder}/DJI_{index + 1:04d}.JPG")
    return paths[:count]


def step_sweep(scratch: Path, root: Path | None) -> dict[str, Any]:
    if root is not None:
        started = time.perf_counter()
        tree = _list_tree(root)
        paths = sorted(
            (
                path.relative_to(root).as_posix()
                for path in tree.files
                if path.suffix.lower() in SUPPORTED
                and not path.relative_to(root)
                .as_posix()
                .upper()
                .startswith(("$RECYCLE.BIN/", "SYSTEM VOLUME INFORMATION/"))
            ),
        )[:ARCHIVE_FILES]
        listed_in = time.perf_counter() - started
        source = f"the real paths of {root} ({len(paths)}, listed in {listed_in:.0f} s)"
    else:
        paths = synthetic_paths(ARCHIVE_FILES)
        source = "synthetic paths shaped like a drone's archive"
    print(f"\nsweep: {len(paths)} rows, {source}", flush=True)
    mean_length = statistics.fmean(len(path) for path in paths)
    cluster = Cluster(scratch)
    device = synthetic_device("VARREDURA", 7)
    try:
        with Session(bind=cluster.engine) as session:
            PostgresDeviceRepository(session).save(device)
            session.commit()
        started = time.perf_counter()
        stamp = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        with psycopg.connect(cluster.connection.conninfo(), autocommit=True) as db:
            db.execute(
                "CREATE TEMP TABLE staging (id uuid, relative_path text, "
                "filename text, "
                "extension text, file_size bigint, file_modified_at timestamptz)"
            )
            with db.cursor().copy("COPY staging FROM STDIN") as copy:
                for index, path in enumerate(paths):
                    relative = ImagePath(path)
                    copy.write_row(
                        (
                            compute_image_id(device.id, relative).value,
                            str(relative),
                            relative.value.stem,
                            relative.value.suffix.lower().lstrip("."),
                            3_800_000 + index,
                            stamp + datetime.timedelta(seconds=index),
                        )
                    )
            # As RFC-045 section 14.6: no HNSW index in this throwaway cluster,
            # and random vectors, so that the table is the size of the real one.
            db.execute("DROP INDEX IF EXISTS ix_images_embedding_hnsw")
            db.execute(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                "extension, file_size, file_modified_at, embedding, capture_source, "
                "position_source) SELECT s.id, %s, s.relative_path, s.filename, "
                "s.extension, s.file_size, s.file_modified_at, "
                "(SELECT array_agg(random()::real) FROM generate_series(1, 512) "
                " WHERE s.id IS NOT NULL)::vector(512), 'unknown', 'unknown' "
                "FROM staging s",
                (device.id.value,),
            )
            db.execute("VACUUM ANALYZE images")
            (table_mb,) = db.execute(
                "SELECT pg_total_relation_size('images') / 1e6"
            ).fetchone()  # type: ignore[misc]
        print(
            f"  written in {time.perf_counter() - started:.0f} s; images is "
            f"{table_mb:.0f} MB",
            flush=True,
        )

        generator = random.Random(SEED)
        missing = set(generator.sample(range(len(paths)), len(paths) // 100))
        listing_files = tuple(p for i, p in enumerate(paths) if i not in missing)

        # Three timed runs, each the first read after a restart of the server,
        # and then one under `tracemalloc` for the memory: tracing every
        # allocation slows the minting of 115,000 ids several times over (the
        # first run of this step timed them traced, and read 10 s of minting
        # where 1.6 s is the cost).
        runs = []
        for attempt in range(4):
            traced = attempt == 3
            # Unmark, so that every run marks the same 1%.
            with cluster.engine.begin() as db:
                db.execute(text("UPDATE images SET missing_since = NULL"))
            cluster.restart()
            gc.collect()
            with Session(bind=cluster.engine) as session:
                repository = PostgresImageRepository(session)
                sweeper = SweepMissingImagesUseCase(repository)
                if traced:
                    tracemalloc.start()
                    base, _ = tracemalloc.get_traced_memory()
                with PeakPrivate() as private:
                    began = time.perf_counter()
                    # What `JobRunner._sweep_missing()` does with a kept listing.
                    listed = {listed_identity(device.id, p)[1] for p in listing_files}
                    minted = time.perf_counter()
                    outcome = sweeper.execute(device.id, listed, ())
                    seconds = time.perf_counter() - began
                    del listed
                if traced:
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
            run: dict[str, Any] = {
                "traced": traced,
                "seconds": round(seconds, 2),
                "minting_seconds": round(minted - began, 2),
                "marked": outcome.marked,
                "private_peak_mb": round((private.peak - private.before) / MB, 1),
            }
            if traced:
                run["python_peak_mb"] = round((peak - base) / MB, 1)
            runs.append(run)
            print(f"  run {attempt + 1}: {json.dumps(runs[-1])}", flush=True)
        return {
            "rows": len(paths),
            "source": source,
            "mean_path_length": round(mean_length, 1),
            "table_mb": round(float(table_mb), 0),
            "missing": len(missing),
            "runs": runs,
            "listing_retained_mb": round(
                sum(sys.getsizeof(p) for p in listing_files) / MB, 1
            ),
        }
    finally:
        cluster.close()


# ---------------------------------------------------------------------------
# listing and rescan: the disk (needs --root)
# ---------------------------------------------------------------------------


def step_listing(root: Path, rounds: int) -> dict[str, Any]:
    print(f"\nlisting: {root}, walk() against rglob(), alternated", flush=True)
    timings: dict[str, list[float]] = {"walk": [], "rglob": []}
    results: dict[str, list[Path]] = {}
    unreadable: list[str] = []
    for round_index in range(rounds):
        order = ("walk", "rglob") if round_index % 2 == 0 else ("rglob", "walk")
        for name in order:
            started = time.perf_counter()
            if name == "walk":
                tree = _list_tree(root)
                entries = tree.entries
                unreadable = [str(p) for p in tree.unreadable]
            else:
                entries = list(root.rglob("*"))
            timings[name].append(time.perf_counter() - started)
            results[name] = sorted(entries, key=discovery_sort_key)
            print(
                f"  round {round_index + 1} {name}: {timings[name][-1]:.1f} s",
                flush=True,
            )
    equal = results["walk"] == results["rglob"]
    phase = listing_phase(root, rounds)
    return {
        "entries": len(results["walk"]),
        "equal_entry_by_entry": equal,
        "unreadable": unreadable,
        "walk_seconds": [round(t, 1) for t in timings["walk"]],
        "rglob_seconds": [round(t, 1) for t in timings["rglob"]],
        "first_is_cold": "walk",
        "phase": phase,
    }


def listing_phase(root: Path, rounds: int) -> dict[str, Any]:
    """The listing as a job of the whole disk sees it, before and after RFC-046.

    Before: `sorted(rglob)` and RFC-039's count, by extension, outside the
    thumbnail cache, through `is_relative_to()`. After: `_list_tree()`, the
    unreadable folders outside the excluded ones, the `sorted()` and the
    `RootListing`, with the cache and the two system folders excluded --
    what `_discover_under()` does before its first file. Alternated.
    """
    from app.infrastructure.filesystem.filesystem_image_provider import (
        FilesystemImageProvider,
    )

    thumbnails = settings.thumbnail_directory.resolve()
    provider = FilesystemImageProvider(
        root,
        settings.supported_extensions,
        excluded_directories=(
            thumbnails,
            *(root / name for name in job_runner_module.SYSTEM_DIRECTORIES),
        ),
    )

    def before() -> int:
        listing = sorted(root.rglob("*"), key=discovery_sort_key)
        return sum(
            1
            for path in listing
            if path.suffix.lower() in SUPPORTED and not path.is_relative_to(thumbnails)
        )

    def after() -> int:
        tree = _list_tree(root)
        unreadable = [f for f in tree.unreadable if not provider._is_excluded(f)]
        listing = sorted(tree.entries, key=discovery_sort_key)
        built = provider._root_listing(listing, tree.file_identities(), unreadable)
        return built.file_count

    timings: dict[str, list[float]] = {"before": [], "after": []}
    counts: dict[str, int] = {}
    for round_index in range(rounds):
        order = ("after", "before") if round_index % 2 == 0 else ("before", "after")
        for name in order:
            started = time.perf_counter()
            counts[name] = before() if name == "before" else after()
            timings[name].append(time.perf_counter() - started)
            print(
                f"  phase round {round_index + 1} {name}: "
                f"{timings[name][-1]:.1f} s ({counts[name]} files)",
                flush=True,
            )
    return {
        "before_seconds": [round(t, 2) for t in timings["before"]],
        "after_seconds": [round(t, 2) for t in timings["after"]],
        "before_files": counts["before"],
        "after_files": counts["after"],
    }


def step_rescan(
    root: Path, sample: int, listing_cold_seconds: float | None
) -> dict[str, Any]:
    print(f"\nrescan: {root}, the per-file cost of a scan with no change", flush=True)
    started = time.perf_counter()
    tree = _list_tree(root)
    listing_seconds = time.perf_counter() - started
    files = [
        path
        for path in tree.files
        if path.suffix.lower() in SUPPORTED
        and not path.relative_to(root)
        .as_posix()
        .upper()
        .startswith(("$RECYCLE.BIN/", "SYSTEM VOLUME INFORMATION/"))
    ]
    chosen = random.Random(SEED + 1).sample(files, min(sample, len(files)))
    costs = []
    for path in chosen:
        began = time.perf_counter()
        path.stat()
        read_exif_facts(path, capture_date=True, position=True)
        costs.append(time.perf_counter() - began)
    per_file = statistics.fmean(costs)
    listing = (
        listing_cold_seconds if listing_cold_seconds is not None else listing_seconds
    )
    hours = (listing + per_file * len(files)) / 3600
    return {
        "files": len(files),
        "sample": len(chosen),
        "per_file_ms": round(per_file * 1000, 1),
        "per_file_ms_median": round(statistics.median(costs) * 1000, 1),
        "listing_seconds_used": round(listing, 1),
        "projected_hours": round(hours, 2),
    }


# ---------------------------------------------------------------------------
# unplug: the pen drive pulled out mid-job (by hand)
# ---------------------------------------------------------------------------


TRIAL_FOLDER = "rfc046-ensaio"
DRIVE_REMOVABLE = 2
"""`GetDriveTypeW`'s answer for a pen drive or a card; an external HDD is fixed."""


def is_removable(root: Path) -> bool:
    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return int(kernel32.GetDriveTypeW(root.anchor)) == DRIVE_REMOVABLE


def ask_to_pull(root: Path) -> None:
    """A message box, on top of every window: pull the pen drive out now."""
    user32 = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
    user32.MessageBoxW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    ]
    exclamation, system_modal, topmost = 0x30, 0x1000, 0x40000
    user32.MessageBoxW(
        None,
        f"Tire o pen drive {root.anchor} agora, direto da USB, sem ejetar.\n\n"
        "A medição continua sozinha quando ele sumir.",
        "RFC-046: pen drive no meio do job",
        exclamation | system_modal | topmost,
    )


def step_unplug(scratch: Path, pen: Path) -> dict[str, Any]:
    """A job of the whole pen drive, pulled out by hand halfway through.

    **Writes only inside `<pen>/rfc046-ensaio/`**, which it creates and which
    must not exist yet; the rest of the pen drive is read, never written. And
    refuses anything but the root of a removable volume -- a pen drive or a
    card -- so that it can be pointed at neither `C:` nor `E:`, which Windows
    reports as fixed.

    A first job of the whole pen drive indexes 40 photos of the trial folder
    (and whatever photos the pen drive already had). Then one of the 40 is
    deleted -- a sweep that ran would mark it -- and 120 more are written, so
    that the second job still has inference to do when, at its first batch,
    it asks for the pen drive to be pulled out and waits for the volume to go.
    RFC-046 section 10 wants that job `failed` and nothing marked.

    The folder stays on the pen drive, to be deleted by hand once it is back.
    """
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.workers.indexing_worker import register_device

    root = pen.resolve()
    if root != Path(root.anchor):
        raise SystemExit(f"--pen must be the root of a volume, like F:/, not {pen}")
    if not is_removable(root):
        raise SystemExit(
            f"{root} is not a removable volume: the trial writes to the disk it "
            "is pointed at, and only a pen drive or a card will do"
        )
    trial = root / TRIAL_FOLDER
    if trial.exists():
        raise SystemExit(f"{trial} already exists: delete it, and run again")

    generator = random.Random(SEED + 2)
    make_photos(trial / "antes", 40, generator, 1)
    print(f"\nunplug: 40 photos written in {trial}", flush=True)

    cluster = Cluster(scratch)
    settings.thumbnail_directory = scratch / "thumbnails"  # type: ignore[misc]
    job_session = Session(bind=cluster.engine)
    image_session = Session(bind=cluster.engine)
    try:
        device, volume = register_device(
            WindowsVolumeIdentityProvider(),
            PostgresDeviceRepository(job_session),
            root=root,
            label="PEN",
        )
        jobs = PostgresIndexingJobRepository(job_session)
        runner = job_runner_module.build_runner(job_session, image_session)
        runner.warm_up_now()

        def new_job() -> IndexingJob:
            return jobs.create(
                IndexingJob(
                    id=JobId.new(),
                    device_id=device.id,
                    created_at=datetime.datetime.now(tz=datetime.UTC),
                )
            )

        def presence() -> dict[str, bool]:
            with Session(bind=cluster.engine) as session:
                rows = PostgresImageRepository(session).list_presence(device.id)
            return {row.relative_path: row.missing for row in rows}

        first = runner.run_job(new_job().id)
        assert first is not None
        rows_after_first = presence()
        print(
            f"  first job: {first.status.value}, {len(rows_after_first)} rows",
            flush=True,
        )

        (trial / "antes" / "DJI_0001.JPG").unlink()
        make_photos(trial / "depois", 120, generator, 1001)

        repository = runner._indexer._repository  # type: ignore[attr-defined]
        original = repository.save_indexed_many
        pulled: list[float] = []

        def wait_for_the_pen(records: Any) -> None:
            if not pulled:
                asked = time.perf_counter()
                print(
                    "\a\n  >>> TIRE O PEN DRIVE AGORA (sem ejetar) <<<\n",
                    flush=True,
                )
                # And a window on top of the others, for whoever is not
                # watching this terminal; it does not hold the wait up.
                threading.Thread(target=ask_to_pull, args=(root,), daemon=True).start()
                while volume.mount_point.exists():
                    time.sleep(0.2)
                pulled.append(time.perf_counter() - asked)
                print(
                    f"  the volume went {pulled[0]:.1f} s after the request",
                    flush=True,
                )
            original(records)

        repository.save_indexed_many = wait_for_the_pen
        started = time.perf_counter()
        finished = runner.run_job(new_job().id)
        seconds = time.perf_counter() - started
        rows = presence()
        return {
            "first_status": first.status.value,
            "rows_after_first": len(rows_after_first),
            "status": finished.status.value if finished else None,
            "error": finished.error_message if finished else None,
            "seconds_to_pull": round(pulled[0], 1) if pulled else None,
            "job_seconds": round(seconds, 1),
            "indexed_before_the_pull": (
                finished.progress.processed_images if finished else None
            ),
            "failed": finished.progress.failed_images if finished else None,
            "rows": len(rows),
            "marked": sum(1 for missing in rows.values() if missing),
        }
    finally:
        job_session.close()
        image_session.close()
        cluster.close()


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--steps", default="tree,search,sweep")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--pen", type=Path)
    parser.add_argument("--search-rounds", type=int, default=4)
    parser.add_argument("--search-queries", type=int, default=50)
    parser.add_argument("--listing-rounds", type=int, default=4)
    parser.add_argument("--rescan-sample", type=int, default=2000)
    parser.add_argument(
        "--json", type=Path, default=Path(__file__).with_name("measure_missing.json")
    )
    args = parser.parse_args()
    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    print(f"RFC-046 measurement, {datetime.datetime.now():%Y-%m-%d %H:%M}, seed {SEED}")
    print(
        f"machine: {platform.platform()}, {platform.processor()}, "
        f"{os.cpu_count()} threads"
    )
    print(
        f"settings: read_ahead {settings.indexing_read_ahead} "
        f"({settings.read_ahead_files} files), batch {settings.batch_size}",
        flush=True,
    )

    report_json: dict[str, Any] = {}
    if args.json.is_file():
        report_json = json.loads(args.json.read_text(encoding="utf-8"))
    report_json["machine"] = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "threads": os.cpu_count(),
    }
    with tempfile.TemporaryDirectory(prefix="rfc046-") as work:
        scratch = Path(work)
        for step in steps:
            started = time.perf_counter()
            step_scratch = scratch / step
            step_scratch.mkdir()
            if step == "tree":
                outcome = step_tree(step_scratch)
            elif step == "search":
                outcome = step_search(
                    step_scratch, args.search_rounds, args.search_queries
                )
            elif step == "sweep":
                outcome = step_sweep(step_scratch, args.root)
            elif step == "listing":
                if args.root is None:
                    raise SystemExit("listing needs --root")
                outcome = step_listing(args.root, args.listing_rounds)
            elif step == "unplug":
                if args.pen is None:
                    raise SystemExit("unplug needs --pen")
                outcome = step_unplug(step_scratch, args.pen)
            elif step == "rescan":
                if args.root is None:
                    raise SystemExit("rescan needs --root")
                cold = report_json.get("listing", {}).get("walk_seconds", [None])[0]
                outcome = step_rescan(args.root, args.rescan_sample, cold)
            else:
                raise SystemExit(f"unknown step {step!r}")
            outcome["date"] = datetime.datetime.now().isoformat(timespec="seconds")
            outcome["step_seconds"] = round(time.perf_counter() - started, 1)
            report_json[step] = outcome
            args.json.write_text(
                json.dumps(report_json, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"step {step} done in {outcome['step_seconds']:.0f} s", flush=True)


if __name__ == "__main__":
    main()
