"""What does `GET /api/v1/devices/{id}/folders` cost, and where does it go?

The numbers RFC-031 left `TBM` for sections 8.2, 8.3 and 16.4-16.7, plus
the two the build prompt adds:

    section 4.4   how much of the route's time is `has_children` -- the
                  field that turns one `readdir` into N+1, and the number
                  that decides whether it survives into RFC-032;
    section 4.11  whether the existing B-tree on
                  `(device_id, relative_path)` is usable for the
                  left-anchored `LIKE` at all. RFC-031 section 15 assumes
                  it is; that is only true in a `C` collation or with
                  `text_pattern_ops`, and the pgvector image initialises
                  its cluster in `en_US.utf8` unless told otherwise. This
                  is `SHOW lc_collate` and `EXPLAIN (ANALYZE, BUFFERS)`,
                  not a reading of the migration.

Six parts:

    1. `lc_collate` and the indexes actually on `images`;
    2. `EXPLAIN (ANALYZE, BUFFERS)` for the grouped count, at the root
       and one level down, over 100,000 rows;
    3. `count_by_path_prefixes()` timed, against N separate `COUNT(*)`s;
    4. the locator's `list_folders()` with and without `has_children`,
       over a 40-folder tree;
    5. `readdir` over a folder with 5,000 entries;
    6. `GET /folders` end to end, plus the size of the JSON it returns --
       which is half the argument about paginating (section 8.3).

**Not measured, and said so in the output: a cold mechanical disk.** The
tree below is built under the system temp directory, on this machine's
SSD, and every folder is read once before timing. RFC-029 section 7.2
found the cold read dominant on an external mechanical disk, and nothing
here contradicts or confirms that -- it is simply a different question.

Run it with the backend virtualenv, from the repository root:

    backend/.venv/Scripts/python.exe \
        experiments/rfc-031-devices-api/measure_folder_listing.py

`RFC031_IMAGES` sets the row count (default 100000); `RFC031_FOLDERS`
the subfolder count (default 40); `RFC031_WIDE` the wide-folder entry
count (default 5000).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, text  # noqa: E402

from app.domain.entities.device import Device  # noqa: E402
from app.domain.services.device_identity import compute_device_id  # noqa: E402
from app.domain.value_objects.device_id import (  # noqa: E402
    VolumeIdentity,
    VolumeKind,
)
from app.domain.value_objects.job_scope import JobScope  # noqa: E402
from app.infrastructure.database.models.device_model import DeviceModel  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.filesystem.mounted_device_locator import (  # noqa: E402
    MountedDeviceLocator,
)
from app.infrastructure.persistence.postgres_device_repository import (  # noqa: E402
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import SessionLocal  # noqa: E402
from app.presentation.api import app  # noqa: E402
from app.presentation.dependencies import get_device_locator  # noqa: E402

IMAGES = int(os.environ.get("RFC031_IMAGES", "100000"))
FOLDERS = int(os.environ.get("RFC031_FOLDERS", "40"))
WIDE = int(os.environ.get("RFC031_WIDE", "5000"))
REPEATS = 20

MEASURE_PREFIX = "rfc031-folders"
MEASURE_IDENTITY = VolumeIdentity(
    value=f"\\\\?\\Volume{{{uuid.UUID(int=0xF031_1000)}}}\\",
    kind=VolumeKind.WINDOWS_VOLUME_GUID,
)
MEASURE_DEVICE_ID = compute_device_id(MEASURE_IDENTITY)


class StubLocator(MountedDeviceLocator):
    """The real locator, pointed at a temporary tree instead of a real disk.

    A subclass overriding only `mount_point()`, so that `resolve_scope()`,
    `list_folders()`, the containment check and the `has_children`
    short-circuit are all **the production code** -- which is the point
    of measuring at all. Replacing the locator wholesale would measure
    the replacement.
    """

    def __init__(self, mount: Path) -> None:
        self._mount = mount

    def mount_point(self, device: Device) -> Path | None:
        return self._mount


def timed(call: Callable[[], object], repeats: int = REPEATS) -> list[float]:
    """Return `repeats` durations in milliseconds, after one warm-up call."""
    call()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def report(label: str, samples: list[float]) -> None:
    print(
        f"  {label:<44} median {statistics.median(samples):8.2f} ms   "
        f"mean {statistics.fmean(samples):8.2f} ms   "
        f"p95 {sorted(samples)[int(len(samples) * 0.95) - 1]:8.2f} ms   "
        f"max {max(samples):8.2f} ms   (n={len(samples)})"
    )


def build_tree(root: Path) -> None:
    """A device root with `FOLDERS` months under `2018`, and one wide folder.

    Each month gets a subfolder, so `has_children` is true for all of
    them and the short-circuit is exercised on its worst case: it stops
    at the first directory, and this way there is always one to find.
    """
    for n in range(FOLDERS):
        (root / "2018" / f"{n:02d}-mes" / "casamento").mkdir(parents=True)
    wide = root / "2018" / "wide"
    wide.mkdir()
    for n in range(WIDE):
        (wide / f"entry_{n:05d}").mkdir()


def seed(count: int) -> Device:
    """Register the measurement device and bulk-insert `count` image rows."""
    now = datetime.datetime.now(tz=datetime.UTC)
    device = Device(
        id=MEASURE_DEVICE_ID,
        volume_identity=MEASURE_IDENTITY,
        label=f"{MEASURE_PREFIX}-device",
        first_seen_at=now,
        last_seen_at=now,
    )
    session = SessionLocal()
    try:
        PostgresDeviceRepository(session).save(device)
        rows = [
            {
                "id": uuid.uuid4(),
                "device_id": device.id.value,
                "relative_path": (
                    f"2018/{n % FOLDERS:02d}-mes/casamento/IMG_{n:06d}.JPG"
                ),
                "filename": f"IMG_{n:06d}",
                "extension": "jpg",
            }
            for n in range(count)
        ]
        session.execute(
            text(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                "extension) VALUES (:id, :device_id, :relative_path, :filename, "
                ":extension)"
            ),
            rows,
        )
        session.commit()
        session.execute(text("ANALYZE images"))
        session.commit()
    finally:
        session.close()
    return device


def cleanup() -> None:
    session = SessionLocal()
    try:
        session.execute(
            delete(ImageModel).where(ImageModel.device_id == MEASURE_DEVICE_ID.value)
        )
        session.execute(
            delete(DeviceModel).where(DeviceModel.id == MEASURE_DEVICE_ID.value)
        )
        session.commit()
    finally:
        session.close()


@contextlib.contextmanager
def overridden(locator: MountedDeviceLocator) -> Iterator[TestClient]:
    app.dependency_overrides[get_device_locator] = lambda: locator
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def grouped_sql(prefix: str) -> tuple[str, dict[str, object]]:
    """The statement `count_by_path_prefixes()` emits, for `EXPLAIN`.

    Written out here rather than rendered from the SQLAlchemy expression
    so that the plan below is readable next to it in the log, and so that
    a reader can paste it into `psql`. It must stay in step with
    `PostgresImageRepository.count_by_path_prefixes()`; part 3 times the
    real method, so a drift would show as a difference between the two.
    """
    if prefix:
        return (
            "SELECT split_part(substr(relative_path, :cut), '/', 1) AS folder, "
            "count(*) FROM images WHERE device_id = :device "
            "AND relative_path LIKE :prefix GROUP BY 1",
            {
                "cut": len(prefix) + 2,
                "device": MEASURE_DEVICE_ID.value,
                "prefix": f"{prefix}/%",
            },
        )
    return (
        "SELECT split_part(substr(relative_path, 1), '/', 1) AS folder, "
        "count(*) FROM images WHERE device_id = :device GROUP BY 1",
        {"device": MEASURE_DEVICE_ID.value},
    )


def main() -> None:
    print(
        f"python {platform.python_version()}, {platform.platform()}, "
        f"{IMAGES} image rows, {FOLDERS} folders, {WIDE}-entry wide folder"
    )
    print(f"processor: {platform.processor()}")
    print()

    cleanup()
    workdir = Path(tempfile.mkdtemp(prefix="rfc031-folders-"))
    try:
        build_tree(workdir)
        device = seed(IMAGES)
        locator = StubLocator(workdir)

        # 1 -------------------------------------------------------------
        print("1. the cluster's collation and the indexes on images")
        session = SessionLocal()
        try:
            # `SHOW lc_collate` was a GUC until PostgreSQL 16 and is gone
            # in 17, which is the version the pgvector image ships. The
            # catalogue is where the answer actually lives, and it is the
            # same answer.
            locale = session.execute(
                text(
                    "SELECT datcollate, datctype, datlocprovider, version() "
                    "FROM pg_database WHERE datname = current_database()"
                )
            ).one()
            print(f"  server: {locale.version.split(' on ')[0]}")
            print(
                f"  datcollate = {locale.datcollate}, "
                f"datctype = {locale.datctype}, "
                f"locale provider = {locale.datlocprovider}"
            )
            print(
                "  a left-anchored LIKE can only use a plain B-tree in the C "
                "collation, or an index built with text_pattern_ops "
                "(RFC-031 section 15 assumed it could)"
            )
            for row in session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE tablename = 'images'"
                )
            ).all():
                print(f"    {row.indexname}: {row.indexdef}")
            print()

            # 2 ---------------------------------------------------------
            print("2. EXPLAIN (ANALYZE, BUFFERS) for the grouped count")
            for label, prefix in (("device root", ""), ("one level down", "2018")):
                statement, params = grouped_sql(prefix)
                print(f"  {label} (prefix={prefix!r}):")
                print(f"    {statement}")
                plan = session.execute(
                    text(f"EXPLAIN (ANALYZE, BUFFERS) {statement}"), params
                ).all()
                for line in plan:
                    print(f"      {line[0]}")
            print()

            # 3 ---------------------------------------------------------
            print("3. count_by_path_prefixes() against one query per folder")
            repository = PostgresImageRepository(session)
            grouped = timed(
                lambda: repository.count_by_path_prefixes(
                    MEASURE_DEVICE_ID, JobScope("2018")
                )
            )
            counts = repository.count_by_path_prefixes(
                MEASURE_DEVICE_ID, JobScope("2018")
            )
            report(f"one grouped query, {len(counts)} folders", grouped)

            def per_folder() -> None:
                for name in counts:
                    session.execute(
                        text(
                            "SELECT count(*) FROM images WHERE device_id = :device "
                            "AND relative_path LIKE :prefix"
                        ),
                        {
                            "device": MEASURE_DEVICE_ID.value,
                            "prefix": f"2018/{name}/%",
                        },
                    ).scalar_one()

            separate = timed(per_folder, repeats=5)
            report(f"{len(counts)} separate COUNT(*) queries", separate)
            print(
                f"  the grouped read is "
                f"{statistics.median(separate) / statistics.median(grouped):.1f}x "
                f"cheaper at {len(counts)} folders"
            )
        finally:
            session.close()
        print()

        # 4 -------------------------------------------------------------
        print("4. what has_children costs the locator")
        scope = JobScope("2018")
        with_children = timed(lambda: locator.list_folders(device, scope))
        report(
            f"list_folders() with has_children ({FOLDERS + 1} folders)",
            with_children,
        )

        def names_only() -> None:
            """The same listing without opening a single child.

            What the route would cost if `has_children` were dropped --
            one `readdir` instead of N+1 (RFC-031 section 4.4 of the
            build prompt).
            """
            with os.scandir(workdir / "2018") as entries:
                [entry.name for entry in entries if entry.is_dir()]

        without = timed(names_only)
        report("the same listing, names only", without)
        added = statistics.median(with_children) - statistics.median(without)
        print(
            f"  has_children adds {added:.2f} ms over {FOLDERS + 1} folders "
            f"({added / (FOLDERS + 1):.3f} ms per folder, "
            f"{100 * added / statistics.median(with_children):.0f}% of the listing), "
            f"warm, on this machine's system drive"
        )
        print()

        # 5 -------------------------------------------------------------
        print("5. a folder with many entries")
        wide_scope = JobScope("2018/wide")
        wide_samples = timed(
            lambda: locator.list_folders(device, wide_scope), repeats=5
        )
        report(f"list_folders() over {WIDE} entries", wide_samples)
        print()

        # 6 -------------------------------------------------------------
        print("6. GET /api/v1/devices/{id}/folders end to end")
        with overridden(locator) as client:
            url = f"/api/v1/devices/{MEASURE_DEVICE_ID}/folders"
            for label, params in (
                ("device root", {}),
                ("2018", {"path": "2018"}),
                (f"2018/wide ({WIDE} entries)", {"path": "2018/wide"}),
            ):
                response = client.get(url, params=params)
                body = response.json()
                repeats = 5 if params.get("path") == "2018/wide" else REPEATS
                samples = timed(
                    lambda url=url, params=params: client.get(url, params=params),
                    repeats=repeats,
                )
                payload = len(json.dumps(body).encode("utf-8"))
                report(f"{label} -> {len(body['folders'])} folders", samples)
                print(
                    f"    response body: {payload / 1024:.1f} KB "
                    f"({payload / max(len(body['folders']), 1):.0f} bytes per folder)"
                )
        print()

        print(
            "NOT MEASURED: a cold mechanical disk. The tree above was built "
            "under the system temp directory on this machine's system drive "
            "and read once before every timing, so these are directory-entry "
            "costs with the OS cache warm. RFC-029 section 7.2 found the cold "
            "read dominant on an external mechanical disk; nothing here "
            "confirms or contradicts that."
        )
    finally:
        cleanup()
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
        print("development database left as it was found")


if __name__ == "__main__":
    main()
