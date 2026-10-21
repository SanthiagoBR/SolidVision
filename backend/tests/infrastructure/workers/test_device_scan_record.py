"""The job of the whole disk writes the denominator of "% indexed" (RFC-039 §4.1).

`last_scan_at` and `last_scan_file_count` existed since RFC-027 and nothing
wrote them, so the percentage RFC-031 section 4.2 publishes the parts of
could never be drawn. The scan that writes them is the one the job already
does: the `sorted(rglob)` of the root, counted when it ends and before the
first file is read.

Two halves, as in the code:

* **the provider** counts its listing and says so through the observer
  port, once, only when it walks the whole root;
* **the executor** checks that the disk is still mounted where the job
  began, and writes the two columns -- never the row, and never by raising
  into the scan.

Real files on a `tmp_path`, as in `test_job_runner.py`; fakes only for the
model, the images and the disk's connection.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePath

import pytest
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
    FakeVolumeCatalog,
    StubContentHasher,
    make_device,
    make_mounted_volume,
)

from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.infrastructure.workers.job_runner import JobRunner
from app.presentation.api import app
from app.presentation.dependencies import get_list_devices_use_case

START = datetime.datetime(2026, 10, 26, 9, 30, tzinfo=datetime.UTC)
SUPPORTED = (".jpg", ".png")
VOLUME = "\\\\?\\Volume{00000000-0000-0000-0000-000000000039}\\"


class ListingObserver(IndexingObserver):
    """Writes down what the scan reports, in the order it happens."""

    def __init__(self) -> None:
        self.listings: list[int] = []
        self.events: list[str] = []

    def listed(self, file_count: int) -> None:
        self.listings.append(file_count)
        self.events.append(f"listed {file_count}")

    def discovered(self, files_seen: int, complete: bool) -> None:
        return None

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def should_stop(self) -> bool:
        return False


@pytest.fixture()
def disk(tmp_path: Path) -> Path:
    """Seven photos in two folders and at the root, and what must not count."""
    root = tmp_path / "disk"
    for folder, count in (("2018", 3), ("2019", 2)):
        (root / folder).mkdir(parents=True)
        for index in range(count):
            (root / folder / f"photo_{index:03d}.jpg").write_bytes(b"data")
    (root / "loose_a.png").write_bytes(b"data")
    (root / "loose_b.JPG").write_bytes(b"data")
    (root / "notes.txt").write_bytes(b"not a photo")
    cache = root / "AppData" / "thumbnails"
    cache.mkdir(parents=True)
    (cache / "ab.jpg").write_bytes(b"a thumbnail of ours")
    return root


def scan(
    root: Path,
    observer: IndexingObserver,
    scopes: tuple[JobScope, ...] = (),
    resume_after: PurePath | None = None,
) -> FilesystemImageProvider:
    return FilesystemImageProvider(
        root,
        SUPPORTED,
        extract_capture_date=False,
        extract_gps=False,
        scopes=scopes,
        resume_after=resume_after,
        observer=observer,
        excluded_directories=(root / "AppData" / "thumbnails",),
    )


class TestTheListing:
    def test_the_whole_root_reports_its_listing_once_before_the_first_file(
        self, disk: Path
    ) -> None:
        observer = ListingObserver()
        files = scan(disk, observer).discover()

        first = next(files)
        observer.events.append(f"file {first.path.name}")
        rest = list(files)

        assert observer.listings == [7]
        assert observer.events[0] == "listed 7"
        assert len(rest) == 6

    def test_the_count_leaves_out_other_files_and_the_excluded_directories(
        self, disk: Path
    ) -> None:
        """`notes.txt` is not a photo, and the thumbnail cache is ours."""
        observer = ListingObserver()

        discovered = list(scan(disk, observer).discover())

        assert observer.listings == [len(discovered)] == [7]

    def test_a_folder_named_like_a_photo_counts_and_is_declared(
        self, disk: Path
    ) -> None:
        """The price of counting without a `stat` (RFC-039 section 4.1).

        The walk sees that `album.jpg` is a directory and yields nothing
        for it; the count, which opens nothing, does not see it.
        """
        (disk / "album.jpg").mkdir()
        observer = ListingObserver()

        discovered = list(scan(disk, observer).discover())

        assert len(discovered) == 7
        assert observer.listings == [8]

    def test_a_scan_of_some_folders_never_reports_a_listing(self, disk: Path) -> None:
        """Two folders are not the size of the disk."""
        observer = ListingObserver()

        discovered = list(
            scan(disk, observer, scopes=(JobScope("2018"), JobScope("2019"))).discover()
        )

        assert len(discovered) == 5
        assert observer.listings == []

    def test_a_resume_counts_the_files_before_its_checkpoint(self, disk: Path) -> None:
        """The listing is always of the whole tree, and so is the denominator."""
        observer = ListingObserver()

        discovered = list(
            scan(disk, observer, resume_after=Path("2019/photo_000.jpg")).discover()
        )

        assert [item.path.name for item in discovered] == [
            "photo_001.jpg",
            "loose_a.png",
            "loose_b.JPG",
        ]
        assert observer.listings == [7]

    def test_an_empty_disk_is_a_listing_of_zero(self, tmp_path: Path) -> None:
        observer = ListingObserver()

        assert list(scan(tmp_path, observer).discover()) == []
        assert observer.listings == [0]

    def test_a_root_that_is_not_there_reports_nothing(self, tmp_path: Path) -> None:
        observer = ListingObserver()

        assert list(scan(tmp_path / "gone", observer).discover()) == []
        assert observer.listings == []


# --------------------------------------------------------------------------
# The executor


class MovableClock:
    def __init__(self, start: datetime.datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now


class MountChangingLocator(FakeDeviceLocator):
    """A disk that is somewhere else -- or nowhere -- once the listing ends.

    The first `mount_point()` is the one `_index()` asks before the scan;
    every one after it answers `after`. That is the disk pulled, or plugged
    back under another letter, while the `rglob` ran.
    """

    def __init__(self, device: Device, before: Path, after: Path | None) -> None:
        super().__init__({device.id: before})
        self._after = after
        self._asked = 0

    def mount_point(self, device: Device) -> Path | None:
        self._asked += 1
        if self._asked == 1:
            return super().mount_point(device)
        return self._after


class RenamingLocator(FakeDeviceLocator):
    """A rename that lands while the listing runs -- through the API, say.

    The second `mount_point()` is the executor's check before it writes the
    scan; the rename is made in the repository just before it answers.
    """

    def __init__(
        self, device: Device, mount: Path, devices: FakeDeviceRepository
    ) -> None:
        super().__init__({device.id: mount})
        self._devices = devices
        self._asked = 0

    def mount_point(self, device: Device) -> Path | None:
        self._asked += 1
        if self._asked == 2:
            stored = self._devices.get(device.id)
            assert stored is not None
            self._devices.save(dataclasses.replace(stored, label="HD2 renomeado"))
        return super().mount_point(device)


class FailingScanRepository(FakeDeviceRepository):
    def record_scan(
        self, device_id: DeviceId, file_count: int, scanned_at: datetime.datetime
    ) -> None:
        raise RuntimeError("the connection went away")


class World:
    def __init__(
        self,
        disk: Path,
        devices: FakeDeviceRepository | None = None,
        locator: FakeDeviceLocator | None = None,
    ) -> None:
        self.clock = MovableClock()
        self.disk = disk
        self.device = make_device(volume_value=VOLUME, label="HD2")
        self.devices = devices or FakeDeviceRepository()
        self.devices.save(self.device)
        self.jobs = InMemoryIndexingJobRepository()
        self.images = FakeImageRepository()
        self.locator = locator or FakeDeviceLocator({self.device.id: disk})
        self.runner = JobRunner(
            jobs=self.jobs,
            devices=self.devices,
            locator=self.locator,
            indexer=IndexOrUpdateImagesUseCase(
                repository=self.images,
                embedding_model=FakeEmbeddingModel(),
                content_hasher=StubContentHasher(),
                batch_size=4,
                metadata_prefetch_size=8,
            ),
            supported_extensions=SUPPORTED,
            extract_capture_date=False,
            extract_gps=False,
            clock=self.clock,
            excluded_directories=(disk / "AppData" / "thumbnails",),
        )

    def run(self, *scopes: str) -> IndexingJob | None:
        job = self.jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=self.device.id,
                scopes=tuple(JobScope(scope) for scope in scopes),
                created_at=self.clock(),
            )
        )
        return self.runner.run_job(job.id)

    def stored(self) -> Device:
        stored = self.devices.get(self.device.id)
        assert stored is not None
        return stored


class TestTheExecutorRecordsTheScan:
    def test_a_job_of_the_whole_disk_writes_the_count_and_the_date(
        self, disk: Path
    ) -> None:
        world = World(disk)

        finished = world.run()

        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert finished.progress.processed_images == 7
        stored = world.stored()
        assert stored.last_scan_file_count == 7
        assert stored.last_scan_at == START

    def test_it_is_written_before_the_first_photo_is(self, disk: Path) -> None:
        """The denominator is the listing's, and the listing is over first."""
        world = World(disk)

        world.run()

        assert world.devices.record_scan_calls == [(world.device.id, 7, START)]
        assert world.images.save_indexed_calls, "the run indexed nothing"

    def test_a_job_of_some_folders_writes_nothing(self, disk: Path) -> None:
        world = World(disk)

        finished = world.run("2018")

        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert world.devices.record_scan_calls == []
        assert world.stored().last_scan_file_count is None
        assert world.stored().last_scan_at is None

    def test_a_disk_pulled_during_the_listing_writes_nothing(self, disk: Path) -> None:
        """The count of a listing over a vanished volume is not the disk's."""
        device = make_device(volume_value=VOLUME, label="HD2")
        world = World(disk, locator=MountChangingLocator(device, disk, None))

        finished = world.run()

        assert world.devices.record_scan_calls == []
        assert world.stored().last_scan_file_count is None
        # And `_completion_of()` sees the same disk gone.
        assert finished is not None
        assert finished.status is JobStatus.FAILED

    def test_a_disk_back_under_another_letter_writes_nothing(
        self, disk: Path, tmp_path: Path
    ) -> None:
        device = make_device(volume_value=VOLUME, label="HD2")
        elsewhere = tmp_path / "another-letter"
        elsewhere.mkdir()
        world = World(disk, locator=MountChangingLocator(device, disk, elsewhere))

        world.run()

        assert world.devices.record_scan_calls == []

    def test_a_record_that_raises_leaves_the_job_to_finish(
        self, disk: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The callback runs inside the scan, where a raise would cut it short."""
        world = World(disk, devices=FailingScanRepository())

        with caplog.at_level(
            logging.WARNING, logger="app.infrastructure.workers.job_progress_observer"
        ):
            finished = world.run()

        assert finished is not None
        assert finished.status is JobStatus.COMPLETED
        assert finished.progress.processed_images == 7
        assert any(
            "Could not record the scan" in record.getMessage()
            for record in caplog.records
        )

    def test_a_rename_made_while_the_disk_was_listed_survives(self, disk: Path) -> None:
        """Two columns, not the row: the executor's copy is from the start."""
        devices = FakeDeviceRepository()
        device = make_device(volume_value=VOLUME, label="HD2")
        world = World(
            disk, devices=devices, locator=RenamingLocator(device, disk, devices)
        )

        world.run()

        stored = world.stored()
        assert stored.label == "HD2 renomeado"
        assert stored.last_scan_file_count == 7

    def test_a_second_job_of_the_whole_disk_moves_the_date(self, disk: Path) -> None:
        world = World(disk)
        world.run()
        (disk / "2019" / "photo_new.jpg").write_bytes(b"data")
        world.clock.now = START + datetime.timedelta(days=3)

        world.run()

        stored = world.stored()
        assert stored.last_scan_file_count == 8
        assert stored.last_scan_at == START + datetime.timedelta(days=3)


# --------------------------------------------------------------------------
# End to end: what GET /devices says after it


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_get_devices_carries_the_scan_of_a_job_of_the_whole_disk(
    disk: Path, client: TestClient
) -> None:
    """The two fields the card divides, written by the job, read by the route."""
    world = World(disk)
    describer = DescribeDevicesUseCase(
        image_repository=world.images,
        job_repository=world.jobs,
        volume_catalog=FakeVolumeCatalog([make_mounted_volume(disk, VOLUME)]),
    )
    app.dependency_overrides[get_list_devices_use_case] = lambda: ListDevicesUseCase(
        device_repository=world.devices, describer=describer
    )

    before = client.get("/api/v1/devices").json()["devices"][0]
    world.run()
    after = client.get("/api/v1/devices").json()["devices"][0]

    assert before["last_scan_file_count"] is None
    assert before["last_scan_at"] is None
    assert after["last_scan_file_count"] == 7
    assert datetime.datetime.fromisoformat(after["last_scan_at"]) == START
    assert after["indexed_images"] == 7
    assert after["id"] == str(world.device.id.value)
    assert uuid.UUID(after["id"]) == world.device.id.value
