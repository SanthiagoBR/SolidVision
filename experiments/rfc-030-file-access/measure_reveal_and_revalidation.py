"""The request-time costs RFC-030 adds, measured through the real routes.

Three `TBM`s of RFC-030 section 12 and one cost the RFC did not list:

    1. `/reveal` guard latency -- the 404 of guard 1, the 403 of guard 2,
       and the 204 of a permitted request. A guard that is slow to refuse
       is a guard someone will be tempted to move behind a cache.
    2. launching a process. The 204 means "launched", so its real cost is
       process creation. **Explorer is not started by this script**: that
       would open windows on the desktop of whoever runs it. What is timed
       instead is `subprocess.Popen` of `cmd.exe /c exit 0` with the same
       arguments -- list form, `shell=False`, no wait -- and it is labelled
       as the stand-in it is.
    3. volume enumeration, which is not in section 12 and is the per-request
       price of RFC-030 section 4.2: search, `GET /images/{id}` and
       `/reveal` each ask the operating system once per distinct disk.
    4. `GET /images/{id}/thumbnail`, 200 against 304 with a current
       `If-None-Match`, through the production dependency graph and a real
       PostgreSQL session. The claim under test is that a 304 reads no
       thumbnail from disk, so it is checked by **counting** -- calls to
       `FilesystemThumbnailStore.locate()` and `FileResponse` constructions
       -- not inferred from the timings.

Every database row is written inside one transaction that is rolled back at
the end, the arrangement `tests/conftest.py`'s `db_session` uses, so the
development database is left as it was found. `TestClient` runs the ASGI app
in-process: the latencies below exclude the network stack, which on
loopback is small but not zero.

Run it with the backend virtualenv, from the repository root:

    backend/.venv/Scripts/python.exe \
        experiments/rfc-030-file-access/measure_reveal_and_revalidation.py
"""

from __future__ import annotations

import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

import logging  # noqa: E402

import fastapi  # noqa: E402
import starlette  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.application.use_cases.resolve_image_location import (  # noqa: E402
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase  # noqa: E402
from app.domain.entities.device import Device  # noqa: E402
from app.domain.entities.image import Image  # noqa: E402
from app.domain.services.device_identity import (  # noqa: E402
    compute_device_id,
)
from app.domain.services.device_locator import DeviceLocator  # noqa: E402
from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind  # noqa: E402
from app.domain.value_objects.embedding_vector import EmbeddingVector  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.domain.value_objects.indexing_record import IndexingRecord  # noqa: E402
from app.domain.value_objects.job_scope import JobScope  # noqa: E402
from app.infrastructure.config.settings import settings  # noqa: E402
from app.infrastructure.database.models.image_model import (  # noqa: E402
    EMBEDDING_DIMENSION,
)
from app.infrastructure.filesystem.file_revealer import (  # noqa: E402
    WindowsFileRevealer,
)
from app.infrastructure.filesystem.image_identity import (  # noqa: E402
    compute_image_id,
)
from app.infrastructure.filesystem.thumbnail_store import (  # noqa: E402
    FilesystemThumbnailStore,
)
from app.infrastructure.filesystem.volume_identity_provider import (  # noqa: E402
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402
from app.infrastructure.persistence.in_memory_device_repository import (  # noqa: E402
    InMemoryDeviceRepository,
)
from app.infrastructure.persistence.in_memory_image_repository import (  # noqa: E402
    InMemoryImageRepository,
)
from app.infrastructure.persistence.postgres_device_repository import (  # noqa: E402
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import get_db  # noqa: E402
from app.presentation.api import app  # noqa: E402
from app.presentation.api.v1.routers import images as images_router  # noqa: E402
from app.presentation.dependencies import get_reveal_image_use_case  # noqa: E402

logging.getLogger("app").setLevel(logging.ERROR)

REQUESTS = 1000
THUMBNAIL_REQUESTS = 500
LOOPBACK = ("127.0.0.1", 51234)
REMOTE = ("192.168.1.20", 51234)


def describe(label: str, samples_ms: Sequence[float]) -> str:
    ordered = sorted(samples_ms)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return (
        f"  {label:<40} median {statistics.median(ordered):7.3f} ms   "
        f"mean {statistics.fmean(ordered):7.3f} ms   p95 {p95:7.3f} ms   "
        f"(n={len(ordered)})"
    )


def timed(action: Callable[[], object], count: int, warm_up: int = 50) -> list[float]:
    for _ in range(warm_up):
        action()
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        action()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


class RecordingLauncher:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1


class FixedLocator(DeviceLocator):
    def __init__(self, mount: Path) -> None:
        self._mount = mount

    def mount_point(self, device: Device) -> Path | None:
        return self._mount

    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        return scope


def measure_guards(workdir: Path) -> None:
    print("1. /reveal guard latency (in-process TestClient, fakes behind the route)")
    mount = workdir / "disk"
    (mount / "fotos").mkdir(parents=True)
    photo = mount / "fotos" / "DJI_0042.JPG"
    photo.write_bytes(b"\xff\xd8")

    identity = VolumeIdentity(
        value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000fe}\\",
        kind=VolumeKind.WINDOWS_VOLUME_GUID,
    )
    device = Device(
        id=compute_device_id(identity), volume_identity=identity, label="HD3"
    )
    devices = InMemoryDeviceRepository()
    devices.save(device)
    images = InMemoryImageRepository()
    relative = ImagePath("fotos/DJI_0042.JPG")
    image = Image(
        id=compute_image_id(device.id, relative),
        device_id=device.id,
        relative_path=relative,
        filename="DJI_0042",
        extension="jpg",
    )
    images.save(image)
    launcher = RecordingLauncher()
    app.dependency_overrides[get_reveal_image_use_case] = lambda: RevealImageUseCase(
        repository=images,
        location_resolver=ResolveImageLocationUseCase(devices, FixedLocator(mount)),
        file_revealer=WindowsFileRevealer(launch=launcher),
    )
    url = f"/api/v1/images/{image.id.value}/reveal"
    unknown = f"/api/v1/images/{uuid.uuid4()}/reveal"
    original = settings.allow_local_file_actions
    try:
        scenarios: list[tuple[str, bool, tuple[str, int], str, int]] = [
            ("guard 1 off, loopback -> 404", False, LOOPBACK, url, 404),
            ("guard 2, LAN caller, setting on -> 403", True, REMOTE, url, 403),
            ("permitted, loopback, setting on -> 204", True, LOOPBACK, url, 204),
            ("permitted, unknown id -> 404", True, LOOPBACK, unknown, 404),
        ]
        for label, enabled, address, target, expected in scenarios:
            settings.allow_local_file_actions = enabled
            with TestClient(app, client=address) as client:
                status = client.post(target).status_code
                assert status == expected, (label, status)
                before = launcher.calls
                samples = timed(lambda: client.post(target), REQUESTS, warm_up=50)
            print(describe(label, samples))
            print(
                f"  {'':<40} processes that would have launched: "
                f"{launcher.calls - before} of {REQUESTS + 50} requests "
                "(timed + warm-up)"
            )
    finally:
        settings.allow_local_file_actions = original
        app.dependency_overrides.clear()
    print()


def measure_process_launch() -> None:
    print("2. launching a process the way the revealer does (stand-in: cmd.exe)")
    command = [
        str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
        "/c",
        "exit 0",
    ]
    launched: list[subprocess.Popen[bytes]] = []

    def launch() -> None:
        launched.append(
            subprocess.Popen(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    samples = timed(launch, 30, warm_up=3)
    for process in launched:
        process.wait()
    print(describe("Popen(list, shell=False), no wait", samples))
    print("  NOT MEASURED: explorer.exe itself -- starting it would open windows.")
    print()


def measure_enumeration() -> None:
    print("3. volume enumeration: the per-disk cost of resolving a location")
    provider = WindowsVolumeIdentityProvider()
    volumes = provider.mounted_volumes()
    samples = timed(provider.mounted_volumes, 200, warm_up=5)
    print(f"  mounted volumes on this machine: {len(volumes)}")
    print(describe("mounted_volumes()", samples))
    print("  a search page asks this once per distinct disk among its hits;")
    print("  GET /images/{id} and /reveal ask it once.")
    print()


def measure_thumbnails(workdir: Path) -> None:
    print(
        "4. GET /images/{id}/thumbnail, production wiring over PostgreSQL "
        "(transaction rolled back at the end)"
    )
    cache = workdir / "cache"
    store = FilesystemThumbnailStore(cache)
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    locate_calls = 0
    file_responses = 0
    real_locate = FilesystemThumbnailStore.locate
    real_file_response = images_router.FileResponse

    def counting_locate(self: FilesystemThumbnailStore, location: str) -> Path | None:
        nonlocal locate_calls
        locate_calls += 1
        return real_locate(self, location)

    class CountingFileResponse(real_file_response):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal file_responses
            file_responses += 1
            super().__init__(*args, **kwargs)

    original_directory = settings.thumbnail_directory
    try:
        identity = VolumeIdentity(
            value="\\\\?\\Volume{00000000-0000-0000-0000-0000000000fd}\\",
            kind=VolumeKind.WINDOWS_VOLUME_GUID,
        )
        device = Device(
            id=compute_device_id(identity), volume_identity=identity, label="HD3"
        )
        PostgresDeviceRepository(session).save(device)
        relative = ImagePath(f"fotos/{uuid.uuid4().hex}.jpg")
        image = Image(
            id=compute_image_id(device.id, relative),
            device_id=device.id,
            relative_path=relative,
            filename="x",
            extension="jpg",
        )
        thumbnail = (
            Path(__file__).resolve().parents[2]
            / "backend"
            / "dataset"
            / "demo"
            / "images"
        )
        sample = next(p for p in sorted(thumbnail.rglob("*.jpg")))
        from app.infrastructure.filesystem.thumbnail_generator import (
            PillowThumbnailGenerator,
        )

        sample_image = Image(
            id=image.id,
            device_id=device.id,
            relative_path=relative,
            filename="x",
            extension="jpg",
            absolute_path=ImagePath(sample),
        )
        location = store.save(
            image.id, PillowThumbnailGenerator().generate(sample_image, 512)
        )
        content_hash = "c" * 64
        PostgresImageRepository(session).save_indexed(
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.1] * EMBEDDING_DIMENSION),
                file_size=1,
                file_modified_at=None,
                content_hash=content_hash,
                thumbnail_path=location,
            )
        )

        settings.thumbnail_directory = cache
        FilesystemThumbnailStore.locate = counting_locate  # type: ignore[method-assign]
        images_router.FileResponse = CountingFileResponse  # type: ignore[misc]
        app.dependency_overrides[get_db] = lambda: session
        url = f"/api/v1/images/{image.id.value}/thumbnail"
        current = {"If-None-Match": f'"{content_hash}"'}
        stale = {"If-None-Match": f'"{"d" * 64}"'}

        with TestClient(app) as client:
            full = client.get(url)
            assert full.status_code == 200
            assert client.get(url, headers=current).status_code == 304
            body_kb = len(full.content) / 1024

            locate_calls = file_responses = 0
            ok = timed(lambda: client.get(url, headers=stale), THUMBNAIL_REQUESTS)
            ok_locates, ok_responses = locate_calls, file_responses

            locate_calls = file_responses = 0
            not_modified = timed(
                lambda: client.get(url, headers=current), THUMBNAIL_REQUESTS
            )
            nm_locates, nm_responses = locate_calls, file_responses

        print(f"  thumbnail served: {body_kb:.1f} KB")
        print(describe("200 (stale If-None-Match)", ok))
        print(
            f"  {'':<40} store.locate calls: {ok_locates}, "
            f"FileResponse built: {ok_responses}"
        )
        print(describe("304 (current If-None-Match)", not_modified))
        print(
            f"  {'':<40} store.locate calls: {nm_locates}, "
            f"FileResponse built: {nm_responses}"
        )
        page = 50 * statistics.median(not_modified)
        print(f"  revalidating a page of 50 thumbnails, sequentially: ~{page:.0f} ms")
    finally:
        FilesystemThumbnailStore.locate = real_locate  # type: ignore[method-assign]
        images_router.FileResponse = real_file_response  # type: ignore[misc]
        settings.thumbnail_directory = original_directory
        app.dependency_overrides.clear()
        session.close()
        transaction.rollback()
        connection.close()
    print("  development database left as it was found")
    print()


def main() -> None:
    print(
        f"python {platform.python_version()}, fastapi {fastapi.__version__}, "
        f"starlette {starlette.__version__}, {platform.platform()}"
    )
    print(f"processor: {platform.processor()}")
    print()
    workdir = Path(tempfile.mkdtemp(prefix="rfc030-http-"))
    try:
        measure_guards(workdir)
        measure_process_launch()
        measure_enumeration()
        measure_thumbnails(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
