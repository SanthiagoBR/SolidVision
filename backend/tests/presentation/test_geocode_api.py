"""`GET /api/v1/geocode` at the HTTP edge (RFC-043), over a fake geocoder.

The production use case with the adapter replaced, so nothing here leaves the
machine. Every client states a loopback address and host, for the reason
`test_capabilities_api.py` gives.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.presentation.dependencies as dependencies_module
from app.domain.services.geocoder_port import GeocoderPort
from app.domain.value_objects.place import Place
from app.domain.value_objects.position import Position
from app.infrastructure.config.settings import settings
from app.infrastructure.geocoding.nominatim_geocoder import (
    AddressSearchUnavailableError,
    NominatimGeocoder,
)
from app.presentation.api import app
from app.presentation.dependencies import get_geocoder

URL = "/api/v1/geocode"
LOOPBACK = ("127.0.0.1", 51234)
REMOTE = ("192.168.1.20", 51234)
ADDRESS = "Rua das Palmeiras, 123, Criciúma, SC"
PALMEIRAS = Place(
    Position(-28.6823, -49.3712),
    "Rua das Palmeiras, Criciúma, Santa Catarina, Brasil",
)


class FakeGeocoder(GeocoderPort):
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.answer: Place | None = PALMEIRAS
        self.unavailable = False

    def geocode(self, address: str) -> Place | None:
        self.asked.append(address)
        if self.unavailable:
            raise AddressSearchUnavailableError(
                "The address search service did not answer (HTTP 503)."
            )
        return self.answer


@pytest.fixture
def geocoder() -> FakeGeocoder:
    return FakeGeocoder()


def client_from(address: tuple[str, int]) -> TestClient:
    return TestClient(app, client=address, base_url="http://127.0.0.1")


@pytest.fixture
def client(
    geocoder: FakeGeocoder, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "allow_address_search", True)
    app.dependency_overrides[get_geocoder] = lambda: geocoder
    with client_from(LOOPBACK) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def app_records(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """The app's loggers do not propagate to the root, where `caplog` listens."""
    logger = logging.getLogger("app")
    previous = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.setLevel(previous)
        logger.removeHandler(caplog.handler)


def test_a_found_address_is_its_point_and_what_the_service_understood(
    client: TestClient, geocoder: FakeGeocoder
) -> None:
    response = client.get(URL, params={"q": f"  {ADDRESS} "})

    assert response.status_code == 200
    assert response.json() == {
        "place": {
            "latitude": -28.6823,
            "longitude": -49.3712,
            "label": "Rua das Palmeiras, Criciúma, Santa Catarina, Brasil",
        }
    }
    assert geocoder.asked == [ADDRESS]


def test_an_address_that_matches_nothing_is_a_200_with_no_place(
    client: TestClient, geocoder: FakeGeocoder
) -> None:
    geocoder.answer = None

    response = client.get(URL, params={"q": "Rua que não existe"})

    assert response.status_code == 200
    assert response.json() == {"place": None}


def test_an_empty_address_is_a_400_and_nothing_is_sent(
    client: TestClient, geocoder: FakeGeocoder
) -> None:
    response = client.get(URL, params={"q": "   "})

    assert response.status_code == 400
    assert geocoder.asked == []


def test_no_address_at_all_is_a_422(client: TestClient) -> None:
    assert client.get(URL).status_code == 422


def test_a_service_that_does_not_answer_is_a_503(
    client: TestClient, geocoder: FakeGeocoder
) -> None:
    geocoder.unavailable = True

    response = client.get(URL, params={"q": ADDRESS})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "The address search service did not answer (HTTP 503)."
    }


def test_switched_off_it_is_a_403_and_the_geocoder_is_never_asked(
    client: TestClient, geocoder: FakeGeocoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "allow_address_search", False)

    response = client.get(URL, params={"q": ADDRESS})

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Address search is switched off in this installation."
    }
    assert geocoder.asked == []


def test_a_caller_from_the_network_is_refused_and_nothing_is_sent(
    geocoder: FakeGeocoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API is never a relay to the service for somebody else."""
    monkeypatch.setattr(settings, "allow_address_search", True)
    app.dependency_overrides[get_geocoder] = lambda: geocoder
    try:
        with client_from(REMOTE) as remote:
            response = remote.get(URL, params={"q": ADDRESS})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert geocoder.asked == []


def test_the_address_is_not_logged(
    client: TestClient, app_records: pytest.LogCaptureFixture
) -> None:
    client.get(URL, params={"q": ADDRESS})

    assert app_records.records, "the fixture must be capturing something"
    for record in app_records.records:
        assert "Palmeiras" not in record.getMessage(), record.name


def test_the_process_has_one_geocoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """One clock for the one-request-per-second rule, and one cache."""
    monkeypatch.setattr(dependencies_module, "_geocoder", None)

    first = get_geocoder()

    assert isinstance(first, NominatimGeocoder)
    assert get_geocoder() is first
