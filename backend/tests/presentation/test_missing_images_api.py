"""The photos a scan no longer found, through the API (RFC-046 sections 5.7 and 5.8).

`DELETE /devices/{id}/missing-images` and the two fields `GET /devices` gained.
The thumbnails live in a real `FilesystemThumbnailStore` under `tmp_path`, so
that "the thumbnails out of the cache" is a file that is no longer there.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceRepository,
    FakeImageRepository,
    FakeVolumeCatalog,
    make_mounted_volume,
)
from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY, make_test_device

from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.purge_missing_images import PurgeMissingImagesUseCase
from app.domain.entities.image import Image
from app.domain.entities.indexing_job import IndexingJob
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.job_id import JobId
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.presentation.api import app
from app.presentation.dependencies import (
    get_list_devices_use_case,
    get_purge_missing_images_use_case,
)

SCAN = datetime.datetime(2026, 3, 12, 10, 30, tzinfo=datetime.UTC)
LATER = datetime.datetime(2026, 3, 20, 10, 30, tzinfo=datetime.UTC)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.mount = tmp_path / "disk"
        self.mount.mkdir()
        self.device = make_test_device("HD3")
        self.devices = FakeDeviceRepository([self.device])
        self.images = FakeImageRepository()
        self.jobs = InMemoryIndexingJobRepository()
        self.thumbnails = FilesystemThumbnailStore(tmp_path / "thumbnails")
        self.catalog = FakeVolumeCatalog(
            [make_mounted_volume(self.mount, TEST_VOLUME_IDENTITY.value)]
        )

    def indexed(self, path: str) -> Image:
        relative = ImagePath(path)
        picture = Image(
            id=compute_image_id(TEST_DEVICE_ID, relative),
            device_id=TEST_DEVICE_ID,
            relative_path=relative,
            filename=relative.value.stem,
            extension="jpg",
        )
        self.images.save_indexed(
            IndexingRecord(
                image=picture,
                embedding=EmbeddingVector([0.1, 0.2]),
                file_size=1,
                file_modified_at=SCAN,
                content_hash="a" * 64,
                thumbnail_path=self.thumbnails.save(picture.id, b"\xff\xd8 jpeg"),
            )
        )
        return picture

    def thumbnail_of(self, picture: Image) -> Path | None:
        metadata = self.images.get_index_metadata(picture.id)
        location = (
            metadata.thumbnail_path
            if metadata is not None and metadata.thumbnail_path is not None
            else f"{picture.id.value.hex[:2]}/{picture.id.value}.jpg"
        )
        return self.thumbnails.locate(location)

    def describer(self) -> DescribeDevicesUseCase:
        return DescribeDevicesUseCase(
            image_repository=self.images,
            job_repository=self.jobs,
            volume_catalog=self.catalog,
        )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    built = World(tmp_path)
    app.dependency_overrides.update(
        {
            get_list_devices_use_case: lambda: ListDevicesUseCase(
                device_repository=built.devices, describer=built.describer()
            ),
            get_purge_missing_images_use_case: lambda: PurgeMissingImagesUseCase(
                image_repository=built.images,
                device_repository=built.devices,
                job_repository=built.jobs,
                thumbnail_store=built.thumbnails,
            ),
        }
    )
    yield built
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1") as started:
        yield started


class TestThePurge:
    def test_200_with_the_number_and_the_thumbnails_out_of_the_cache(
        self, world: World, client: TestClient
    ) -> None:
        kept = world.indexed("a/kept.jpg")
        first = world.indexed("a/gone-1.jpg")
        second = world.indexed("a/gone-2.jpg")
        world.images.mark_presence([first.id, second.id], [], SCAN)
        assert world.thumbnail_of(first) is not None

        response = client.delete(f"/api/v1/devices/{TEST_DEVICE_ID}/missing-images")

        assert response.status_code == 200
        assert response.json() == {"purged": 2}
        assert world.images.get(first.id) is None
        assert world.thumbnail_of(first) is None
        assert world.thumbnail_of(second) is None
        assert world.thumbnail_of(kept) is not None

    def test_nothing_marked_is_a_200_of_zero(
        self, world: World, client: TestClient
    ) -> None:
        world.indexed("a/kept.jpg")

        response = client.delete(f"/api/v1/devices/{TEST_DEVICE_ID}/missing-images")

        assert (response.status_code, response.json()) == (200, {"purged": 0})

    def test_404_for_a_device_that_does_not_exist(
        self, world: World, client: TestClient
    ) -> None:
        response = client.delete(
            "/api/v1/devices/00000000-0000-0000-0000-00000000dead/missing-images"
        )

        assert response.status_code == 404

    def test_409_with_an_active_job_and_nothing_deleted(
        self, world: World, client: TestClient
    ) -> None:
        gone = world.indexed("a/gone.jpg")
        world.images.mark_presence([gone.id], [], SCAN)
        world.jobs.create(
            IndexingJob(id=JobId.new(), device_id=TEST_DEVICE_ID, created_at=SCAN)
        )

        response = client.delete(f"/api/v1/devices/{TEST_DEVICE_ID}/missing-images")

        assert response.status_code == 409
        assert world.images.get(gone.id) is not None
        assert world.thumbnail_of(gone) is not None

    def test_the_disk_does_not_have_to_be_connected(
        self, world: World, client: TestClient
    ) -> None:
        gone = world.indexed("a/gone.jpg")
        world.images.mark_presence([gone.id], [], SCAN)
        world.catalog.detach(TEST_VOLUME_IDENTITY)

        response = client.delete(f"/api/v1/devices/{TEST_DEVICE_ID}/missing-images")

        assert (response.status_code, response.json()) == (200, {"purged": 1})


class TestTheDeviceFields:
    def test_a_disk_with_nothing_missing(
        self, world: World, client: TestClient
    ) -> None:
        world.indexed("a/kept.jpg")

        (device,) = client.get("/api/v1/devices").json()["devices"]

        assert device["indexed_images"] == 1
        assert device["missing_images"] == 0
        assert device["missing_since"] is None

    def test_the_number_and_the_oldest_mark(
        self, world: World, client: TestClient
    ) -> None:
        world.indexed("a/kept.jpg")
        first = world.indexed("a/gone-1.jpg")
        second = world.indexed("a/gone-2.jpg")
        world.images.mark_presence([second.id], [], LATER)
        world.images.mark_presence([first.id], [], SCAN)

        (device,) = client.get("/api/v1/devices").json()["devices"]

        assert device["indexed_images"] == 1, "the numerator counts what is visible"
        assert device["missing_images"] == 2
        assert datetime.datetime.fromisoformat(device["missing_since"]) == SCAN

    def test_after_the_purge_the_card_has_no_missing_line(
        self, world: World, client: TestClient
    ) -> None:
        gone = world.indexed("a/gone.jpg")
        world.images.mark_presence([gone.id], [], SCAN)

        client.delete(f"/api/v1/devices/{TEST_DEVICE_ID}/missing-images")
        (device,) = client.get("/api/v1/devices").json()["devices"]

        assert (device["missing_images"], device["missing_since"]) == (0, None)
