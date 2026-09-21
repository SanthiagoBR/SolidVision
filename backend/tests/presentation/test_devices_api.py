"""The device, volume and folder routes end to end (RFC-031).

HTTP-level assertions only: status codes, body shapes, and the two places
where the wire format is itself a decision -- the canonical spelling
echoed back by `/folders`, and the `422` that `PATCH` owes a body with an
extra field in it.

Everything about *how many queries* a route costs is asserted one layer
down, in `tests/application/test_device_use_cases.py`, where the doubles
count. Repeating it here would test the overrides.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
    FakeVolumeCatalog,
    make_mounted_volume,
)
from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY, make_test_device

from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.list_device_folders import ListDeviceFoldersUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.list_mounted_volumes import ListMountedVolumesUseCase
from app.application.use_cases.rename_device import RenameDeviceUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.presentation.api import app
from app.presentation.dependencies import (
    get_describe_devices_use_case,
    get_list_device_folders_use_case,
    get_list_devices_use_case,
    get_list_mounted_volumes_use_case,
    get_rename_device_use_case,
)


class World:
    """One device called HD3, a disk under `tmp_path`, and a folder tree."""

    def __init__(self, tmp_path: Path) -> None:
        self.mount = tmp_path / "disk"
        (self.mount / "Fotos" / "2018" / "janeiro").mkdir(parents=True)
        (self.mount / "Fotos" / "2018" / "fevereiro").mkdir()
        self.device = make_test_device("HD3")
        self.devices = FakeDeviceRepository([self.device])
        self.images = FakeImageRepository()
        self.jobs = InMemoryIndexingJobRepository()
        self.locator = FakeDeviceLocator({TEST_DEVICE_ID: self.mount})
        self.catalog = FakeVolumeCatalog(
            [
                make_mounted_volume(
                    self.mount,
                    TEST_VOLUME_IDENTITY.value,
                    filesystem_label="Seagate Backup",
                    total_bytes=2_000_398_934_016,
                )
            ]
        )

    def index(self, relative_path: str) -> None:
        self.images.save(
            Image(
                id=ImageId(uuid.uuid4()),
                device_id=TEST_DEVICE_ID,
                relative_path=ImagePath(relative_path),
                filename=relative_path.rsplit("/", 1)[-1],
                extension="jpg",
            )
        )

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
            get_describe_devices_use_case: built.describer,
            get_list_devices_use_case: lambda: ListDevicesUseCase(
                device_repository=built.devices, describer=built.describer()
            ),
            get_list_mounted_volumes_use_case: lambda: ListMountedVolumesUseCase(
                volume_catalog=built.catalog, device_repository=built.devices
            ),
            get_rename_device_use_case: lambda: RenameDeviceUseCase(
                device_repository=built.devices, describer=built.describer()
            ),
            get_list_device_folders_use_case: lambda: ListDeviceFoldersUseCase(
                device_repository=built.devices,
                image_repository=built.images,
                job_repository=built.jobs,
                device_locator=built.locator,
            ),
        }
    )
    yield built
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as started:
        yield started


class TestListDevices:
    def test_it_returns_the_device_with_everything_resolved(
        self, world: World, client: TestClient
    ) -> None:
        world.index("Fotos/2018/janeiro/a.jpg")

        payload = client.get("/api/v1/devices").json()

        (device,) = payload["devices"]
        assert device["id"] == str(TEST_DEVICE_ID)
        assert device["label"] == "HD3"
        assert device["connected"] is True
        assert device["mount_point"] == world.mount.as_posix()
        assert device["indexed_images"] == 1
        assert device["active_job"] is None

    def test_mount_point_uses_forward_slashes(
        self, world: World, client: TestClient
    ) -> None:
        """Every path this API publishes is `/`-separated, this one included."""
        (device,) = client.get("/api/v1/devices").json()["devices"]

        assert "\\" not in device["mount_point"]

    def test_a_disconnected_disk_is_reported_not_hidden(
        self, world: World, client: TestClient
    ) -> None:
        """`connected: false` is the answer, not the failure (RFC-030 §4.1)."""
        world.catalog.detach(TEST_VOLUME_IDENTITY)

        (device,) = client.get("/api/v1/devices").json()["devices"]

        assert device["connected"] is False
        assert device["mount_point"] is None
        assert device["label"] == "HD3"

    def test_no_body_carries_a_percentage(
        self, world: World, client: TestClient
    ) -> None:
        (device,) = client.get("/api/v1/devices").json()["devices"]

        assert not any("percent" in key for key in device)


class TestListVolumes:
    def test_a_registered_volume_carries_its_device_id(
        self, world: World, client: TestClient
    ) -> None:
        (volume,) = client.get("/api/v1/volumes").json()["volumes"]

        assert volume["device_id"] == str(TEST_DEVICE_ID)
        assert volume["volume_identity"] == TEST_VOLUME_IDENTITY.value
        assert volume["volume_kind"] == "windows-volume-guid"
        assert volume["filesystem_label"] == "Seagate Backup"
        assert volume["total_bytes"] == 2_000_398_934_016

    def test_an_unregistered_volume_carries_null(
        self, world: World, client: TestClient
    ) -> None:
        """What lets the 'add a device' screen grey out what is already added."""
        world.catalog.attach(
            make_mounted_volume(
                Path("/mnt/new"),
                "\\\\?\\Volume{11111111-1111-1111-1111-111111111111}\\",
            )
        )

        volumes = client.get("/api/v1/volumes").json()["volumes"]

        by_identity = {v["volume_identity"]: v["device_id"] for v in volumes}
        assert by_identity[TEST_VOLUME_IDENTITY.value] == str(TEST_DEVICE_ID)
        assert (
            by_identity["\\\\?\\Volume{11111111-1111-1111-1111-111111111111}\\"] is None
        )


class TestRenameDevice:
    def test_it_renames_and_returns_the_device(
        self, world: World, client: TestClient
    ) -> None:
        response = client.patch(
            f"/api/v1/devices/{TEST_DEVICE_ID}", json={"label": "HD3 — aéreas 2018"}
        )

        assert response.status_code == 200
        assert response.json()["label"] == "HD3 — aéreas 2018"
        stored = world.devices.get(TEST_DEVICE_ID)
        assert stored is not None
        assert stored.label == "HD3 — aéreas 2018"

    def test_it_works_with_the_disk_in_a_drawer(
        self, world: World, client: TestClient
    ) -> None:
        """RFC-031 section 7: renaming is a fact about our table."""
        world.catalog.detach(TEST_VOLUME_IDENTITY)

        response = client.patch(
            f"/api/v1/devices/{TEST_DEVICE_ID}", json={"label": "HD3 guardado"}
        )

        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert response.json()["label"] == "HD3 guardado"

    def test_a_body_with_an_extra_field_is_a_422_and_the_row_is_intact(
        self, world: World, client: TestClient
    ) -> None:
        """`extra="forbid"`, and the reason it is on this one schema.

        Pydantic's default would answer 200 and drop the field, leaving
        the client believing it had edited the declared denominator of "%
        indexed" (RFC-031 section 7).
        """
        response = client.patch(
            f"/api/v1/devices/{TEST_DEVICE_ID}",
            json={"label": "HD3 novo", "last_scan_file_count": 1},
        )

        assert response.status_code == 422
        stored = world.devices.get(TEST_DEVICE_ID)
        assert stored is not None
        assert stored.label == "HD3"

    @pytest.mark.parametrize("label", ["", "   ", "\t\n"])
    def test_a_blank_label_is_refused(
        self, world: World, client: TestClient, label: str
    ) -> None:
        """A disk named with three spaces is a sidebar row with a blank line."""
        response = client.patch(
            f"/api/v1/devices/{TEST_DEVICE_ID}", json={"label": label}
        )

        assert response.status_code == 422
        stored = world.devices.get(TEST_DEVICE_ID)
        assert stored is not None
        assert stored.label == "HD3"

    def test_a_label_is_stored_trimmed(self, world: World, client: TestClient) -> None:
        client.patch(f"/api/v1/devices/{TEST_DEVICE_ID}", json={"label": "  HD9  "})

        stored = world.devices.get(TEST_DEVICE_ID)
        assert stored is not None
        assert stored.label == "HD9"

    def test_an_unknown_device_is_a_404(self, world: World, client: TestClient) -> None:
        response = client.patch(
            f"/api/v1/devices/{uuid.uuid4()}", json={"label": "nope"}
        )

        assert response.status_code == 404


class TestListFolders:
    def test_it_lists_one_level(self, world: World, client: TestClient) -> None:
        payload = client.get(
            f"/api/v1/devices/{TEST_DEVICE_ID}/folders", params={"path": "Fotos/2018"}
        ).json()

        assert payload["device_id"] == str(TEST_DEVICE_ID)
        assert payload["path"] == "Fotos/2018"
        assert payload["parent"] == "Fotos"
        assert [folder["name"] for folder in payload["folders"]] == [
            "fevereiro",
            "janeiro",
        ]

    def test_the_root_is_listed_without_a_path(
        self, world: World, client: TestClient
    ) -> None:
        payload = client.get(f"/api/v1/devices/{TEST_DEVICE_ID}/folders").json()

        assert payload["path"] == ""
        assert payload["parent"] == ""
        assert [folder["name"] for folder in payload["folders"]] == ["Fotos"]

    def test_it_echoes_the_canonical_spelling_not_the_one_requested(
        self, world: World, client: TestClient
    ) -> None:
        """RFC-031 section 8: what comes back is what goes into `scopes[]`.

        The folder on disk is `Fotos`. A client that asked for `FOTOS`
        and got its own spelling back would send it to `POST /jobs`,
        which compares path parts exactly.
        """
        payload = client.get(
            f"/api/v1/devices/{TEST_DEVICE_ID}/folders", params={"path": "FOTOS"}
        ).json()

        assert payload["path"] == "Fotos"
        assert [folder["path"] for folder in payload["folders"]] == ["Fotos/2018"]

    def test_every_path_uses_forward_slashes(
        self, world: World, client: TestClient
    ) -> None:
        payload = client.get(
            f"/api/v1/devices/{TEST_DEVICE_ID}/folders", params={"path": "Fotos/2018"}
        ).json()

        assert "\\" not in payload["path"]
        assert all("\\" not in folder["path"] for folder in payload["folders"])

    def test_a_disconnected_disk_is_a_409_naming_the_disk(
        self, world: World, client: TestClient
    ) -> None:
        world.locator.disconnect(TEST_DEVICE_ID)

        response = client.get(f"/api/v1/devices/{TEST_DEVICE_ID}/folders")

        assert response.status_code == 409
        assert "HD3" in response.json()["detail"]

    @pytest.mark.parametrize(
        "path", ["..", "../../windows", "C:\\Windows", "//server/share", "/etc"]
    )
    def test_a_path_that_escapes_the_device_is_a_400(
        self, world: World, client: TestClient, path: str
    ) -> None:
        response = client.get(
            f"/api/v1/devices/{TEST_DEVICE_ID}/folders", params={"path": path}
        )

        assert response.status_code == 400
        assert world.locator.list_folders_calls == []

    def test_an_unknown_device_is_a_404(self, world: World, client: TestClient) -> None:
        response = client.get(f"/api/v1/devices/{uuid.uuid4()}/folders")

        assert response.status_code == 404

    def test_a_folder_reports_its_state_and_its_exact_count(
        self, world: World, client: TestClient
    ) -> None:
        world.index("Fotos/2018/janeiro/a.jpg")
        world.index("Fotos/2018/janeiro/b.jpg")

        payload = client.get(
            f"/api/v1/devices/{TEST_DEVICE_ID}/folders", params={"path": "Fotos/2018"}
        ).json()

        by_name = {folder["name"]: folder for folder in payload["folders"]}
        assert by_name["janeiro"]["indexed_images"] == 2
        assert by_name["janeiro"]["state"] == "partial"
        assert by_name["fevereiro"]["indexed_images"] == 0
        assert by_name["fevereiro"]["state"] == "never_indexed"

    def test_no_folder_body_carries_a_percentage_or_a_scan_count(
        self, world: World, client: TestClient
    ) -> None:
        payload = client.get(
            f"/api/v1/devices/{TEST_DEVICE_ID}/folders", params={"path": "Fotos/2018"}
        ).json()

        for folder in payload["folders"]:
            assert not any("percent" in key for key in folder)
            assert "file_count" not in folder
            assert "scan_file_count" not in folder
