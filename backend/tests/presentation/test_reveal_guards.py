"""`POST /api/v1/images/{id}/reveal` and the two guards in front of it (RFC-030 §5, §6).

**Read this before adding a test here: `TestClient(app)` is not loopback.**
Starlette's test client reports its peer as `("testclient", 50000)`, which is
not an IP address at all. So the "non-loopback is refused" case is the
*default*, and passes without any effort -- while a test of the permitted
path that forgets `client=("127.0.0.1", ...)` is refused by guard 2 and never
exercises the route. The happy path is therefore written first, and every
client below states its address.

**Nothing here launches a process.** The revealer is the real
`WindowsFileRevealer` with its launcher replaced by a recorder, so the
command it *would* run is asserted on -- list form, no shell, the path the
server built -- and Explorer is never started.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
)
from tests.conftest import TEST_DEVICE_ID, make_test_device

from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.config.settings import Settings, settings
from app.infrastructure.filesystem.file_revealer import WindowsFileRevealer
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.presentation.api import app
from app.presentation.api.v1.routers import images as images_router
from app.presentation.dependencies import get_reveal_image_use_case
from app.presentation.local_file_actions import is_loopback_address

RELATIVE = "fotos/2018/junho/DJI_0042.JPG"
LOOPBACK = ("127.0.0.1", 51234)
REMOTE = ("192.168.1.20", 51234)


class RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.mount = tmp_path / "disk"
        self.mount.mkdir()
        self.images = FakeImageRepository()
        self.locator = FakeDeviceLocator({TEST_DEVICE_ID: self.mount})
        self.launcher = RecordingLauncher()
        self.use_case_built = 0
        self.image = self._index()

    def _index(self) -> Image:
        relative = ImagePath(RELATIVE)
        image = Image(
            id=compute_image_id(TEST_DEVICE_ID, relative),
            device_id=TEST_DEVICE_ID,
            relative_path=relative,
            filename="DJI_0042",
            extension="jpg",
        )
        self.images.save_indexed(
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.1] * 8),
                file_size=1,
                file_modified_at=None,
            )
        )
        photo = self.mount / RELATIVE
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b"\xff\xd8")
        return image

    @property
    def photo(self) -> Path:
        return self.mount / RELATIVE

    def build_use_case(self) -> RevealImageUseCase:
        self.use_case_built += 1
        return RevealImageUseCase(
            repository=self.images,
            location_resolver=ResolveImageLocationUseCase(
                FakeDeviceRepository([make_test_device("HD3")]), self.locator
            ),
            file_revealer=WindowsFileRevealer(launch=self.launcher),
        )

    def url(self, image_id: object | None = None) -> str:
        return f"/api/v1/images/{image_id or self.image.id.value}/reveal"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    built = World(tmp_path)
    app.dependency_overrides[get_reveal_image_use_case] = built.build_use_case
    yield built
    app.dependency_overrides.clear()


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 1 switched on, for one test only.

    `monkeypatch`, so the setting is restored however the test ends: a
    test that turned it on and leaked would make every guard-1 test after
    it pass for the wrong reason (build prompt, invariant 5).
    """
    monkeypatch.setattr(settings, "allow_local_file_actions", True)


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 1 off *explicitly*, not by trusting the environment's default."""
    monkeypatch.setattr(settings, "allow_local_file_actions", False)


def client_from(address: tuple[str, int]) -> TestClient:
    return TestClient(app, client=address)


class TestThePermittedPathActuallyPassesBothGuards:
    """Written first on purpose; see the module docstring."""

    def test_loopback_with_the_setting_on_is_a_204_and_launches_explorer(
        self, world: World, enabled: None
    ) -> None:
        with client_from(LOOPBACK) as client:
            response = client.post(world.url())

        assert response.status_code == 204
        assert response.content == b""
        ((args, kwargs),) = world.launcher.calls
        (command,) = args
        assert command[1:] == ["/select,", str(world.photo)]
        assert kwargs["shell"] is False

    @pytest.mark.parametrize("host", ["::1", "::ffff:127.0.0.1", "127.8.9.10"])
    def test_every_loopback_spelling_is_accepted(
        self, world: World, enabled: None, host: str
    ) -> None:
        with client_from((host, 51234)) as client:
            assert client.post(world.url()).status_code == 204


class TestGuardOneTheSetting:
    def test_it_defaults_to_off(self) -> None:
        """The precedent of `warm_up_models`: nothing acts unless asked to."""
        assert Settings.model_fields["allow_local_file_actions"].default is False

    def test_off_is_a_404_even_from_loopback_and_launches_nothing(
        self, world: World, disabled: None
    ) -> None:
        with client_from(LOOPBACK) as client:
            response = client.post(world.url())

        assert response.status_code == 404
        assert world.launcher.calls == []

    def test_off_looks_exactly_like_a_route_that_does_not_exist(
        self, world: World, disabled: None
    ) -> None:
        """RFC-030 section 6: not existing, rather than existing and refusing."""
        with client_from(LOOPBACK) as client:
            refused = client.post(world.url())
            absent = client.post(f"/api/v1/images/{uuid.uuid4()}/no-such-action")

        assert refused.status_code == absent.status_code == 404
        assert refused.json() == absent.json()

    def test_off_never_builds_the_use_case(self, world: World, disabled: None) -> None:
        """The guard resolves before anything opens a session or enumerates a disk."""
        with client_from(LOOPBACK) as client:
            client.post(world.url())

        assert world.use_case_built == 0

    def test_off_wins_over_a_remote_caller(self, world: World, disabled: None) -> None:
        """A remote probe learns nothing: 404, not the 403 that admits a route."""
        with client_from(REMOTE) as client:
            assert client.post(world.url()).status_code == 404


class TestGuardTwoLoopback:
    def test_a_lan_caller_is_refused_even_with_the_setting_on(
        self, world: World, enabled: None
    ) -> None:
        """The operator who started the API on 0.0.0.0 without noticing."""
        with client_from(REMOTE) as client:
            response = client.post(world.url())

        assert response.status_code == 403
        assert world.launcher.calls == []
        assert world.use_case_built == 0

    def test_the_default_test_client_is_refused_too(
        self, world: World, enabled: None
    ) -> None:
        """`("testclient", 50000)` is not an address, and "unknown" is not "local"."""
        with TestClient(app) as client:
            assert client.post(world.url()).status_code == 403
        assert world.launcher.calls == []

    @pytest.mark.parametrize(
        "host",
        ["192.168.1.20", "10.0.0.5", "::ffff:192.168.0.1", "fe80::1", "localhost", ""],
    )
    def test_only_literal_loopback_addresses_count(self, host: str) -> None:
        assert is_loopback_address(host) is False

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.255.0.1", "::1"])
    def test_loopback_addresses_do(self, host: str) -> None:
        assert is_loopback_address(host) is True


class TestNoPathEverEntersTheRoute:
    """RFC-030 section 5.1, as a property of the route's signature."""

    def reveal_route(self) -> APIRoute:
        """Read off the images router itself, where the route is declared.

        Not off `app.routes`: this FastAPI includes routers lazily, so the
        application's own list holds wrappers rather than the routes.
        """
        (route,) = [
            route
            for route in images_router.router.routes
            if isinstance(route, APIRoute) and route.path.endswith("/reveal")
        ]
        return route

    def test_the_handler_takes_an_id_and_a_use_case_and_nothing_else(self) -> None:
        parameters = inspect.signature(images_router.reveal_image).parameters

        assert list(parameters) == ["image_id", "use_case"]

    def test_the_only_client_input_is_the_id_in_the_url(self) -> None:
        route = self.reveal_route()
        dependant = route.dependant

        assert [param.name for param in dependant.path_params] == ["image_id"]
        assert dependant.query_params == []
        assert dependant.header_params == []
        assert dependant.cookie_params == []
        assert dependant.body_params == []
        for sub in dependant.dependencies:
            assert sub.query_params == []
            assert sub.header_params == []
            assert sub.body_params == []

    def test_the_published_contract_has_no_request_body(self) -> None:
        operation = app.openapi()["paths"]["/api/v1/images/{image_id}/reveal"]["post"]

        assert "requestBody" not in operation
        assert [parameter["name"] for parameter in operation["parameters"]] == [
            "image_id"
        ]

    def test_a_path_smuggled_into_a_body_or_query_is_ignored(
        self, world: World, enabled: None
    ) -> None:
        with client_from(LOOPBACK) as client:
            response = client.post(
                world.url(),
                params={"path": "C:/Windows/System32/cmd.exe"},
                json={"path": "\\\\attacker\\share\\evil.exe"},
            )

        assert response.status_code == 204
        ((args, _),) = world.launcher.calls
        (command,) = args
        assert command[1:] == ["/select,", str(world.photo)]
        assert "System32" not in command[2]
        assert "attacker" not in command[2]

    def test_it_is_a_post(self) -> None:
        """RFC-030 section 5.2: a GET could be prefetched into ten windows."""
        assert self.reveal_route().methods == {"POST"}


class TestWhatAPermittedRequestCanStillBeTold:
    def test_an_unknown_image_is_a_404_that_says_so(
        self, world: World, enabled: None
    ) -> None:
        with client_from(LOOPBACK) as client:
            response = client.post(world.url(uuid.uuid4()))

        assert response.status_code == 404
        assert "No indexed image" in response.json()["detail"]
        assert world.launcher.calls == []

    def test_a_disconnected_disk_is_a_409_naming_the_disk(
        self, world: World, enabled: None
    ) -> None:
        """Through `DeviceNotConnectedError`, reused rather than recreated."""
        world.locator.disconnect(TEST_DEVICE_ID)

        with client_from(LOOPBACK) as client:
            response = client.post(world.url())

        assert response.status_code == 409
        assert "HD3" in response.json()["detail"]
        assert world.launcher.calls == []

    def test_a_file_gone_from_a_connected_disk_is_a_410(
        self, world: World, enabled: None
    ) -> None:
        world.photo.unlink()

        with client_from(LOOPBACK) as client:
            response = client.post(world.url())

        assert response.status_code == 410
        assert "HD3" in response.json()["detail"]
        assert world.launcher.calls == []

    def test_a_malformed_id_is_a_422_before_anything_happens(
        self, world: World, enabled: None
    ) -> None:
        with client_from(LOOPBACK) as client:
            response = client.post("/api/v1/images/..%2F..%2Fwindows/reveal")

        assert response.status_code in (404, 422)
        assert world.launcher.calls == []


def test_every_guard_test_restores_the_setting() -> None:
    """Runs last in this file: nothing above may have leaked guard 1 on.

    Compared with a freshly loaded `Settings()` rather than with `False`,
    because an operator's `.env` is allowed to turn it on; what must not
    happen is a test changing it for the tests that follow.
    """
    assert settings.allow_local_file_actions == Settings().allow_local_file_actions
