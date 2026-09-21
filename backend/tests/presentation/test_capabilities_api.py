"""`GET /api/v1/capabilities` and its single guard (RFC-031 section 9).

**Read this before adding a test here: `TestClient(app)` is not
loopback.** Starlette's test client reports its peer as
`("testclient", 50000)`, which is not an IP address at all -- so the
"refused" case passes with no effort while a test of the permitted path
that forgets `client=("127.0.0.1", ...)` is refused by the guard and
exercises nothing. The happy path is therefore written first and every
client below states its address. The same warning is at the top of
`test_reveal_guards.py`, which is where it was learned.

The property this file exists to hold is the **asymmetry**: this route
carries `require_loopback_client` and deliberately *not*
`require_local_file_actions_enabled`. Adding the second would be the
tidy-looking change that breaks it -- with the configuration off the
route would vanish, and "off" is precisely the answer the UI came for.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.infrastructure.config.settings import Settings, settings
from app.presentation.api import app
from app.presentation.api.v1.routers import capabilities as capabilities_router
from app.presentation.local_file_actions import (
    require_local_file_actions_enabled,
    require_loopback_client,
)

URL = "/api/v1/capabilities"
LOOPBACK = ("127.0.0.1", 51234)
REMOTE = ("192.168.1.20", 51234)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local file actions on, for one test only, restored however it ends."""
    monkeypatch.setattr(settings, "allow_local_file_actions", True)


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off *explicitly*, not by trusting the environment's default."""
    monkeypatch.setattr(settings, "allow_local_file_actions", False)


def client_from(address: tuple[str, int]) -> TestClient:
    return TestClient(app, client=address)


class TestTheLocalClientGetsAnAnswer:
    def test_loopback_with_the_setting_on_says_so(self, enabled: None) -> None:
        with client_from(LOOPBACK) as client:
            response = client.get(URL)

        assert response.status_code == 200
        assert response.json() == {
            "local_file_actions": True,
            "platform": sys.platform,
            "version": settings.project_version,
        }

    def test_loopback_with_the_setting_off_is_a_200_saying_false(
        self, disabled: None
    ) -> None:
        """**Not a 404** (RFC-031 section 9.1), and this is the whole point.

        `/reveal` answers 404 with the configuration off, by design. If
        this route did the same it could never deliver the one answer the
        UI needs -- that the button should not be drawn -- and the UI
        would be back to discovering it by clicking.
        """
        with client_from(LOOPBACK) as client:
            response = client.get(URL)

        assert response.status_code == 200
        assert response.json()["local_file_actions"] is False

    def test_the_version_is_the_package_version_not_the_sprint_number(self) -> None:
        """RFC-031 section 9's example shows `0.5.0`; that is the sprint.

        `settings.project_version` is `0.1.0` and `pyproject.toml`
        agrees. The setting is reported as it stands rather than the
        project being renumbered to match an illustration.
        """
        assert settings.project_version == Settings().project_version

    @pytest.mark.parametrize("host", ["::1", "::ffff:127.0.0.1", "127.8.9.10"])
    def test_every_loopback_spelling_is_accepted(
        self, enabled: None, host: str
    ) -> None:
        with client_from((host, 51234)) as client:
            assert client.get(URL).status_code == 200


class TestEverybodyElseLearnsNothing:
    def test_a_lan_caller_is_refused_with_the_setting_on(self, enabled: None) -> None:
        with client_from(REMOTE) as client:
            response = client.get(URL)

        assert response.status_code == 403

    def test_a_lan_caller_is_refused_with_the_setting_off_too(
        self, disabled: None
    ) -> None:
        """The refusal must not depend on the configuration it protects."""
        with client_from(REMOTE) as client:
            assert client.get(URL).status_code == 403

    def test_the_refused_body_carries_no_configuration_at_all(
        self, disabled: None
    ) -> None:
        """Not the `false`, not the platform, not the version.

        A 403 whose body still said `local_file_actions: false` would
        leak exactly what the guard is here to withhold -- and it is the
        kind of thing a helpful error message adds without anyone
        noticing.
        """
        with client_from(REMOTE) as client:
            body = client.get(URL).text

        assert "local_file_actions" not in body
        assert sys.platform not in body
        assert settings.project_version not in body

    def test_the_default_test_client_is_refused_too(self, enabled: None) -> None:
        """`("testclient", 50000)` is not an address, and "unknown" is not "local"."""
        with TestClient(app) as client:
            assert client.get(URL).status_code == 403


class TestTheGuardsOnTheRoute:
    def capabilities_route(self) -> APIRoute:
        (route,) = [
            route
            for route in capabilities_router.router.routes
            if isinstance(route, APIRoute) and route.path == "/capabilities"
        ]
        return route

    def _dependency_calls(self) -> list[object]:
        return [
            dependency.call
            for dependency in self.capabilities_route().dependant.dependencies
        ]

    def test_it_carries_the_loopback_guard(self) -> None:
        assert require_loopback_client in self._dependency_calls()

    def test_it_does_not_carry_the_configuration_guard(self) -> None:
        """The asymmetry, asserted so that "fixing" it fails loudly.

        With guard 1 attached, the route would answer 404 exactly when it
        needs to answer `false`, and the UI would have no way to ask the
        question at all (RFC-031 section 9.1).
        """
        assert require_local_file_actions_enabled not in self._dependency_calls()

    def test_it_takes_no_client_input_of_any_kind(self) -> None:
        """Three constants in, nothing from the request but the caller's address."""
        dependant = self.capabilities_route().dependant

        assert dependant.path_params == []
        assert dependant.query_params == []
        assert dependant.header_params == []
        assert dependant.cookie_params == []
        assert dependant.body_params == []

    def test_it_publishes_exactly_three_fields(self) -> None:
        """The bar for a fourth is "the UI draws differently because of it"."""
        schema = app.openapi()["components"]["schemas"]["CapabilitiesSchema"]

        assert set(schema["properties"]) == {
            "local_file_actions",
            "platform",
            "version",
        }


def test_every_capabilities_test_restores_the_setting() -> None:
    """Runs last in this file: nothing above may have leaked the setting on.

    Compared with a freshly loaded `Settings()` rather than with `False`,
    because an operator's `.env` is allowed to turn it on; what must not
    happen is a test changing it for the tests that follow.
    """
    assert settings.allow_local_file_actions == Settings().allow_local_file_actions
