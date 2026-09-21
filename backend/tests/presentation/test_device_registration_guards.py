r"""`POST /api/v1/devices` never accepts a path (RFC-031 section 5.1).

The sibling of `test_reveal_guards.py`, and **not a copy of it**, because
the property is not the same one. `/reveal` asserts *there is no input
beyond the id*, down to `dependant.body_params == []`. This route has a
body -- registering a disk requires the client to say which disk -- so
that assertion is not merely inapplicable, it is false, and a copy of it
would either fail or, "fixed" by deleting the line, assert nothing.

What holds here instead is *no input is a location*:

* the handler's parameters are exactly the four names below, two of
  which FastAPI injects and a client cannot reach;
* the identity arrives **only** in the body -- no path, query, header or
  cookie parameter exists on the route or on its dependencies;
* the body model's fields are exactly three, asserted as a set equality
  rather than with `in`, so that a `path` added later fails here;
* the published OpenAPI schema carries those three properties and no
  others;
* and, behaviourally: a body with a `path` in it is ignored, and an
  identity nobody is holding resolves to **nothing** -- 409, with
  `save()` never called. That last one is the whole argument. An invented
  identity is not a place. An invented path is.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceRepository,
    FakeImageRepository,
    FakeVolumeCatalog,
    make_mounted_volume,
)
from tests.conftest import TEST_DEVICE_ID, TEST_VOLUME_IDENTITY

from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.infrastructure.persistence.in_memory_indexing_job_repository import (
    InMemoryIndexingJobRepository,
)
from app.presentation.api import app
from app.presentation.api.v1.routers import devices as devices_router
from app.presentation.dependencies import (
    get_describe_devices_use_case,
    get_register_device_use_case,
)
from app.presentation.schemas.device_schema import RegisterDeviceRequestSchema

URL = "/api/v1/devices"
UNKNOWN_IDENTITY = "\\\\?\\Volume{deadbeef-0000-0000-0000-000000000000}\\"


class World:
    """One mounted volume, and a record of everything that was asked."""

    def __init__(self, tmp_path: Path, mounted: bool = True) -> None:
        self.mount = tmp_path / "disk"
        self.mount.mkdir()
        self.devices = FakeDeviceRepository()
        self.catalog = FakeVolumeCatalog(
            [
                make_mounted_volume(
                    self.mount,
                    TEST_VOLUME_IDENTITY.value,
                    filesystem_label="Seagate Backup",
                    total_bytes=2_000_398_934_016,
                )
            ]
            if mounted
            else []
        )

    def build_use_case(self) -> RegisterDeviceUseCase:
        return RegisterDeviceUseCase(
            volume_catalog=self.catalog, device_repository=self.devices
        )

    def build_describer(self) -> DescribeDevicesUseCase:
        return DescribeDevicesUseCase(
            image_repository=FakeImageRepository(),
            job_repository=InMemoryIndexingJobRepository(),
            volume_catalog=self.catalog,
        )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    built = World(tmp_path)
    app.dependency_overrides[get_register_device_use_case] = built.build_use_case
    app.dependency_overrides[get_describe_devices_use_case] = built.build_describer
    yield built
    app.dependency_overrides.clear()


@pytest.fixture
def empty_world(tmp_path: Path) -> Iterator[World]:
    """Nothing mounted, so no identity resolves to anything."""
    built = World(tmp_path, mounted=False)
    app.dependency_overrides[get_register_device_use_case] = built.build_use_case
    app.dependency_overrides[get_describe_devices_use_case] = built.build_describer
    yield built
    app.dependency_overrides.clear()


def body(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "volume_identity": TEST_VOLUME_IDENTITY.value,
        "volume_kind": "windows-volume-guid",
        "label": "HD3",
    }
    payload.update(overrides)
    return payload


def register_route() -> APIRoute:
    """Read the route off the devices router, where it is declared.

    Not off `app.routes`: this FastAPI includes routers lazily, so the
    application's own list holds wrappers rather than routes.
    """
    (route,) = [
        route
        for route in devices_router.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/devices"
        and route.methods == {"POST"}
    ]
    return route


class TestNoPathEverEntersTheRoute:
    def test_the_handler_takes_a_body_two_injections_and_nothing_else(self) -> None:
        parameters = inspect.signature(devices_router.register_device).parameters

        assert list(parameters) == ["request", "response", "use_case", "describer"]

    def test_the_identity_arrives_only_in_the_body(self) -> None:
        """No path, query, header or cookie parameter, on the route or below it."""
        dependant = register_route().dependant

        assert dependant.path_params == []
        assert dependant.query_params == []
        assert dependant.header_params == []
        assert dependant.cookie_params == []
        for sub in dependant.dependencies:
            assert sub.path_params == []
            assert sub.query_params == []
            assert sub.header_params == []

    def test_the_body_has_exactly_three_fields(self) -> None:
        """Set equality, not `in`: a `path` added later has to fail here."""
        assert set(RegisterDeviceRequestSchema.model_fields) == {
            "volume_identity",
            "volume_kind",
            "label",
        }

    def test_the_published_contract_publishes_only_those_three(self) -> None:
        schema = app.openapi()["components"]["schemas"]["RegisterDeviceRequestSchema"]

        assert set(schema["properties"]) == {
            "volume_identity",
            "volume_kind",
            "label",
        }

    def test_no_field_of_the_request_is_named_like_a_location(self) -> None:
        """A blunt check, and the one that would catch a careless rename."""
        for name in RegisterDeviceRequestSchema.model_fields:
            assert not any(
                word in name for word in ("path", "root", "dir", "folder", "file")
            )

    def test_a_path_smuggled_into_the_body_is_ignored(self, world: World) -> None:
        with TestClient(app) as client:
            response = client.post(
                URL, json=body(path="C:\\Windows\\System32", root="D:\\fotos")
            )

        assert response.status_code == 201
        (saved,) = world.devices.save_calls
        # The root the registration used is the mount point the *server*
        # enumerated, and nothing the client sent reached a filesystem.
        assert saved.filesystem_label == "Seagate Backup"
        assert "System32" not in str(saved.volume_identity.value)

    def test_an_invented_identity_resolves_to_nothing(self, empty_world: World) -> None:
        """The heart of RFC-031 section 5.1, as one assertion.

        A path a client invents *is* a location whether or not anybody
        meant it to be. An identity a client invents is looked up in a
        mapping the server produced seconds ago, and what is not in it
        does not exist -- so the failure mode is a 409 and an untouched
        database rather than a read of somebody's `C:` drive.
        """
        with TestClient(app) as client:
            response = client.post(URL, json=body(volume_identity=UNKNOWN_IDENTITY))

        assert response.status_code == 409
        assert empty_world.devices.save_calls == []


class TestRegistrationOutcomes:
    def test_a_first_registration_is_a_201(self, world: World) -> None:
        with TestClient(app) as client:
            response = client.post(URL, json=body())

        assert response.status_code == 201
        payload = response.json()
        assert payload["id"] == str(TEST_DEVICE_ID)
        assert payload["label"] == "HD3"
        assert payload["connected"] is True
        assert payload["mount_point"] == world.mount.as_posix()

    def test_registering_twice_is_201_then_200_with_one_device(
        self, world: World
    ) -> None:
        """RFC-031 section 6.1: all three claims in one request pair."""
        with TestClient(app) as client:
            first = client.post(URL, json=body())
            second = client.post(URL, json=body(label="HD3 — aéreas"))

        assert first.status_code == 201
        assert second.status_code == 200
        assert len(world.devices.list()) == 1
        assert world.devices.list()[0].label == "HD3 — aéreas"

    def test_both_statuses_are_in_the_published_contract(self) -> None:
        """A client deciding whether something was created reads this."""
        operation = app.openapi()["paths"]["/api/v1/devices"]["post"]

        assert "201" in operation["responses"]
        assert "200" in operation["responses"]

    def test_an_unknown_volume_kind_is_a_400_not_a_422(self, world: World) -> None:
        """RFC-031 section 6, and the reason the schema keeps `str`.

        A `volume_kind: VolumeKind` annotation would have FastAPI refuse
        this with a 422 before the use case ran, and
        `InvalidVolumeIdentityError` would never be raised at all.
        """
        with TestClient(app) as client:
            response = client.post(URL, json=body(volume_kind="btrfs-uuid"))

        assert response.status_code == 400
        assert world.devices.save_calls == []

    def test_an_empty_identity_is_a_400(self, world: World) -> None:
        with TestClient(app) as client:
            response = client.post(URL, json=body(volume_identity="   "))

        assert response.status_code == 400
        assert world.devices.save_calls == []

    def test_a_missing_identity_is_a_422(self, world: World) -> None:
        """The distinction `error_handlers.py` draws: malformed, not refused."""
        with TestClient(app) as client:
            response = client.post(URL, json={"volume_kind": "windows-volume-guid"})

        assert response.status_code == 422
        assert world.devices.save_calls == []

    def test_a_label_is_optional(self, world: World) -> None:
        with TestClient(app) as client:
            response = client.post(
                URL,
                json={
                    "volume_identity": TEST_VOLUME_IDENTITY.value,
                    "volume_kind": "windows-volume-guid",
                },
            )

        assert response.status_code == 201
        assert response.json()["label"] == "Seagate Backup"


class TestTheResponseNeverCarriesAPercentage:
    """RFC-031 section 4.2, asserted on the contract rather than on one body."""

    def test_no_device_schema_publishes_a_percentage(self) -> None:
        schema = app.openapi()["components"]["schemas"]["DeviceDetailSchema"]

        assert "percent_indexed" not in schema["properties"]
        assert not any("percent" in name for name in schema["properties"])

    def test_it_publishes_the_three_parts_instead(self) -> None:
        """The numerator, the denominator and the date the denominator is from."""
        schema = app.openapi()["components"]["schemas"]["DeviceDetailSchema"]

        for field in ("indexed_images", "last_scan_file_count", "last_scan_at"):
            assert field in schema["properties"]

    def test_no_folder_schema_publishes_a_percentage_or_a_file_count(self) -> None:
        schema = app.openapi()["components"]["schemas"]["FolderSchema"]

        assert not any("percent" in name for name in schema["properties"])
        assert "file_count" not in schema["properties"]
        assert "scan_file_count" not in schema["properties"]
        assert "indexed_images" in schema["properties"]


def test_an_unknown_device_id_shape_is_refused_before_anything_happens() -> None:
    with TestClient(app) as client:
        response = client.patch(
            f"/api/v1/devices/{uuid.uuid4()}x", json={"label": "nope"}
        )

    assert response.status_code == 422
