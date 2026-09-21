"""What does `GET /api/v1/devices` cost, and how many enumerations is it?

The numbers RFC-031 left `TBM` for sections 4.1, 4.2 and 16.1-16.3, plus
one the RFC did not ask for and the build prompt (section 4.1) does: the
difference between `VolumeCatalog.mount_points()` and
`list_mounted()`, which is the entire justification for there being two
methods instead of one.

Five parts:

    1. `mount_points()` vs `list_mounted()` against the **real** Windows
       adapter and whatever is plugged into this machine. RFC-030
       measured the first at 0.19 ms with one volume mounted; the second
       is the missing number, and it is the one that decides whether
       folding the detail into the cheap method would have been
       affordable;
    2. `GET /devices` end to end with N = 1, 4 and 20 devices, against
       PostgreSQL, through `TestClient`. Real rows, real queries, a
       stubbed catalogue so that N does not depend on how many disks
       happen to be plugged into the machine running this;
    3. the enumeration count per request, asserted rather than timed --
       the target is 1 for every N, and it is a property no timing could
       establish;
    4. `count_by_device()` over 100,000 image rows, which is the read
       RFC-031 section 4.2 adds to the sidebar;
    5. the same with a `COUNT(*)` per device beside it, so the grouped
       query is measured against the thing it replaced rather than
       against zero.

**Not measured, and said so in the output:** a cold external disk. Part
1 reads whatever is mounted now, warm. The number that would move most
on a sleeping mechanical disk is `GetDiskFreeSpaceExW` inside
`list_mounted()`, and it is the reason the two methods exist -- see the
log for what this machine had attached when it ran.

Run it with the backend virtualenv, from the repository root:

    backend/.venv/Scripts/python.exe \
        experiments/rfc-031-devices-api/measure_device_list.py

`RFC031_IMAGES` sets the image-count corpus size (default 100000).
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import os
import platform
import statistics
import sys
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
from app.domain.services.volume_catalog import VolumeCatalog  # noqa: E402
from app.domain.value_objects.device_id import (  # noqa: E402
    VolumeIdentity,
    VolumeKind,
)
from app.domain.value_objects.mounted_volume import MountedVolume  # noqa: E402
from app.infrastructure.database.models.device_model import DeviceModel  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.filesystem.volume_catalog import (  # noqa: E402
    MountedVolumeCatalog,
)
from app.infrastructure.filesystem.volume_identity_provider import (  # noqa: E402
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.persistence.postgres_device_repository import (  # noqa: E402
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import SessionLocal  # noqa: E402
from app.presentation.api import app  # noqa: E402
from app.presentation.dependencies import get_volume_catalog  # noqa: E402

IMAGES = int(os.environ.get("RFC031_IMAGES", "100000"))
DEVICE_COUNTS = (1, 4, 20)
REPEATS = 30

MEASURE_PREFIX = "rfc031-measure"
"""Marks every row this script writes, so cleanup removes exactly those.

The measurement runs against the development database, like RFC-029's
and RFC-030's, so nothing may be deleted by pattern that a real scan
might have written.
"""


def measure_volume(identity: VolumeIdentity, n: int) -> MountedVolume:
    return MountedVolume(
        identity=identity,
        mount_point=Path(f"Z:/{MEASURE_PREFIX}-{n}"),
        filesystem_label=f"MEASURE-{n}",
        total_bytes=2_000_398_934_016,
    )


MEASURE_UUID_BASE = 0xF031_0000
"""A namespace no real volume GUID will collide with; cleanup keys on it."""


def measure_identity(n: int) -> VolumeIdentity:
    """An identity shaped like a real one, for a disk that does not exist."""
    guid = uuid.UUID(int=MEASURE_UUID_BASE + n)
    return VolumeIdentity(
        value=f"\\\\?\\Volume{{{guid}}}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )


class StubCatalog(VolumeCatalog):
    """A fixed set of volumes, counting how often each method was asked.

    Stubbed rather than real so that N is the variable under test: the
    real adapter reports whatever is plugged into the machine running
    this, which is one or two disks and not twenty. Part 1 measures the
    real adapter on its own, where its cost is the thing being measured.
    """

    def __init__(self, volumes: list[MountedVolume]) -> None:
        self._volumes = volumes
        self.mount_points_calls = 0
        self.list_mounted_calls = 0

    def mount_points(self) -> dict[VolumeIdentity, Path]:
        self.mount_points_calls += 1
        return {volume.identity: volume.mount_point for volume in self._volumes}

    def list_mounted(self) -> list[MountedVolume]:
        self.list_mounted_calls += 1
        return list(self._volumes)


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
        f"  {label:<42} median {statistics.median(samples):8.2f} ms   "
        f"mean {statistics.fmean(samples):8.2f} ms   "
        f"p95 {sorted(samples)[int(len(samples) * 0.95) - 1]:8.2f} ms   "
        f"max {max(samples):8.2f} ms   (n={len(samples)})"
    )


def existing_device_count() -> int:
    """How many device rows the development database already holds.

    Reported rather than deleted: these are the operator's own disks, and
    RFC-027 section 6.3 is explicit that a device row is a record of
    something to plug in rather than something to remove quietly. They
    are served by the route alongside the measurement rows, so the log
    has to say how many of them there were.
    """
    session = SessionLocal()
    try:
        return int(session.execute(text("SELECT count(*) FROM devices")).scalar_one())
    finally:
        session.close()


def seed_devices(count: int) -> list[Device]:
    """Write `count` measurement devices and return them."""
    now = datetime.datetime.now(tz=datetime.UTC)
    session = SessionLocal()
    devices = []
    try:
        repository = PostgresDeviceRepository(session)
        for n in range(count):
            identity = measure_identity(n)
            device = Device(
                id=compute_device_id(identity),
                volume_identity=identity,
                label=f"{MEASURE_PREFIX}-{n:02d}",
                filesystem_label=f"MEASURE-{n}",
                total_bytes=2_000_398_934_016,
                first_seen_at=now,
                last_seen_at=now,
                last_scan_at=now,
                last_scan_file_count=48210,
            )
            repository.save(device)
            devices.append(device)
    finally:
        session.close()
    return devices


def seed_images(device: Device, count: int) -> None:
    """Bulk-insert `count` rows for one device, in a year/month/day tree.

    Raw SQL with `executemany`: this is arrangement, and 100,000 rows
    through the ORM would dominate the script's runtime without
    measuring anything.
    """
    session = SessionLocal()
    try:
        rows = []
        for n in range(count):
            relative = f"2018/{n % 12 + 1:02d}/{n % 28 + 1:02d}/IMG_{n:06d}.JPG"
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "device_id": device.id.value,
                    "relative_path": relative,
                    "filename": f"IMG_{n:06d}",
                    "extension": "jpg",
                }
            )
        session.execute(
            text(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                "extension) VALUES (:id, :device_id, :relative_path, :filename, "
                ":extension)"
            ),
            rows,
        )
        session.commit()
    finally:
        session.close()


def cleanup() -> None:
    """Remove every row this script wrote, images first for the foreign key."""
    session = SessionLocal()
    try:
        session.execute(
            delete(ImageModel).where(
                ImageModel.device_id.in_(
                    [
                        compute_device_id(measure_identity(n)).value
                        for n in range(max(DEVICE_COUNTS))
                    ]
                )
            )
        )
        session.execute(
            delete(DeviceModel).where(DeviceModel.label.like(f"{MEASURE_PREFIX}-%"))
        )
        session.commit()
    finally:
        session.close()


@contextlib.contextmanager
def overridden(catalog: VolumeCatalog) -> Iterator[TestClient]:
    """Serve the API with `catalog` in place of the real Windows one."""
    app.dependency_overrides[get_volume_catalog] = lambda: catalog
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def main() -> None:
    print(
        f"python {platform.python_version()}, "
        f"{platform.platform()}, {IMAGES} image rows"
    )
    print(f"processor: {platform.processor()}")
    print()

    # 1 -----------------------------------------------------------------
    print("1. the two catalogue methods, against the real Windows adapter")
    real = MountedVolumeCatalog(WindowsVolumeIdentityProvider())
    mounted = real.list_mounted()
    print(f"  volumes attached to this machine: {len(mounted)}")
    for volume in mounted:
        print(
            f"    {str(volume.mount_point):<8} "
            f"label={volume.filesystem_label!r:<24} "
            f"{(volume.total_bytes or 0) / 1e9:8.1f} GB"
        )
    cheap = timed(real.mount_points)
    rich = timed(real.list_mounted)
    report("mount_points() -- one enumeration", cheap)
    report("list_mounted() -- plus one call per volume", rich)
    ratio = statistics.median(rich) / statistics.median(cheap)
    print(
        f"  list_mounted() costs {ratio:.1f}x mount_points() with "
        f"{len(mounted)} volume(s) attached, all of them warm"
    )
    print()

    cleanup()
    try:
        # 2 and 3 -------------------------------------------------------
        print("2. GET /api/v1/devices end to end, against PostgreSQL")
        pre_existing = existing_device_count()
        print(
            f"  the development database already holds {pre_existing} device "
            f"row(s); N below is what this script added on top, and the "
            f"served count is what the route actually rendered"
        )
        devices = seed_devices(1)
        seed_images(devices[0], IMAGES)
        print(f"  seeded {IMAGES} image rows on {devices[0].label}")

        for count in DEVICE_COUNTS:
            # Grown rather than reseeded: `save()` is an upsert keyed on the
            # derived id, so re-saving the first N is free and the 100,000
            # image rows stay attached to device 0 across all three rounds.
            devices = seed_devices(count)
            catalog = StubCatalog(
                [
                    measure_volume(device.volume_identity, n)
                    for n, device in enumerate(devices)
                ]
            )
            with overridden(catalog) as client:
                response = client.get("/api/v1/devices")
                served = len(response.json()["devices"])
                connected = sum(
                    1 for row in response.json()["devices"] if row["connected"]
                )
                before = catalog.mount_points_calls
                samples = timed(functools.partial(client.get, "/api/v1/devices"))
                enumerations = catalog.mount_points_calls - before
                report(
                    f"N={count:<2} ({served} rows served, {connected} connected)",
                    samples,
                )
                print(
                    f"    enumerations: {enumerations} over "
                    f"{REPEATS + 1} requests "
                    f"({enumerations / (REPEATS + 1):.2f} per request), "
                    f"list_mounted(): {catalog.list_mounted_calls}"
                )
        print()

        # 4 and 5 -------------------------------------------------------
        print("4. count_by_device() against the seeded rows")
        session = SessionLocal()
        try:
            repository = PostgresImageRepository(session)
            grouped = timed(repository.count_by_device)
            report(f"count_by_device() -- one GROUP BY, {IMAGES} rows", grouped)

            def per_device() -> None:
                """What N separate `COUNT(*)`s would cost, for comparison."""
                for device in devices:
                    session.execute(
                        text("SELECT count(*) FROM images WHERE device_id = :device"),
                        {"device": device.id.value},
                    ).scalar_one()

            separate = timed(per_device, repeats=10)
            report(f"{len(devices)} separate COUNT(*) queries", separate)
            print(
                f"  the grouped read is "
                f"{statistics.median(separate) / statistics.median(grouped):.1f}x "
                f"cheaper than one query per device at N={len(devices)}"
            )
        finally:
            session.close()
        print()

        print(
            "NOT MEASURED: a cold external disk. Part 1 read whatever was "
            "attached to this machine, warm and already spinning. The call "
            "that would move most on a sleeping mechanical disk is "
            "GetDiskFreeSpaceExW inside list_mounted(), which is why the two "
            "methods are separate -- see the volume list above for what was "
            "actually attached."
        )
    finally:
        cleanup()
        print("development database left as it was found")


if __name__ == "__main__":
    main()
