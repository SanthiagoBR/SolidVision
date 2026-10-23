"""`NominatimGeocoder` over a fake opener: no request leaves this machine.

What the usage policy asks (one request per second, a User-Agent naming the
app, caching) and what RFC-026 section 16.1 asks (the address written nowhere)
are each held by a test here.
"""

from __future__ import annotations

import email.message
import io
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from app.domain.value_objects.place import Place
from app.domain.value_objects.position import Position
from app.infrastructure.geocoding.nominatim_geocoder import (
    CACHE_SIZE,
    MIN_INTERVAL_S,
    TIMEOUT_S,
    AddressSearchUnavailableError,
    NominatimGeocoder,
)

URL = "https://nominatim.example/search"
ADDRESS = "Rua das Palmeiras, 123, Criciúma, SC"
ANSWER = [
    {
        "lat": "-28.6823",
        "lon": "-49.3712",
        "display_name": "Rua das Palmeiras, Criciúma, Santa Catarina, Brasil",
    }
]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Body:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content

    def __enter__(self) -> Body:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def json_body(value: object) -> Body:
    return Body(json.dumps(value).encode("utf-8"))


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{URL}?q=Rua+das+Palmeiras",
        code,
        "error",
        email.message.Message(),
        io.BytesIO(),
    )


class Service:
    """The fake Nominatim: records each request and answers with `respond`."""

    def __init__(self, respond: Callable[[urllib.request.Request], Any]) -> None:
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self._respond = respond

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self._respond(request)


def answering(value: object) -> Service:
    return Service(lambda request: json_body(value))


def failing(error: BaseException) -> Service:
    def respond(request: urllib.request.Request) -> Any:
        raise error

    return Service(respond)


def in_turn(*outcomes: BaseException | object) -> Service:
    queue = iter(outcomes)

    def respond(request: urllib.request.Request) -> Any:
        outcome = next(queue)
        if isinstance(outcome, BaseException):
            raise outcome
        return json_body(outcome)

    return Service(respond)


def geocoder_for(service: Service, clock: FakeClock | None = None) -> NominatimGeocoder:
    clock = clock or FakeClock()
    return NominatimGeocoder(
        URL, "1.2.3", opener=service, clock=clock, sleep=clock.sleep
    )


@pytest.fixture
def every_record(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[pytest.LogCaptureFixture]:
    """The app's records, which do not propagate, and the root's -- every level."""
    caplog.set_level(logging.DEBUG)
    app_logger = logging.getLogger("app")
    previous = app_logger.level
    app_logger.addHandler(caplog.handler)
    app_logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        app_logger.setLevel(previous)
        app_logger.removeHandler(caplog.handler)


# --------------------------------------------------------------------------
# The request, and the answer
# --------------------------------------------------------------------------


def test_the_request_asks_for_one_result_in_brazil_naming_the_app() -> None:
    service = answering(ANSWER)

    geocoder_for(service).geocode("  Rua das Palmeiras,   123 ")

    (request,) = service.requests
    url = urllib.parse.urlsplit(request.full_url)
    assert f"{url.scheme}://{url.netloc}{url.path}" == URL
    assert urllib.parse.parse_qs(url.query) == {
        "q": ["Rua das Palmeiras, 123"],
        "format": ["jsonv2"],
        "limit": ["1"],
        "countrycodes": ["br"],
        "accept-language": ["pt-BR"],
    }
    assert request.get_header("User-agent") == (
        "SolidVision/1.2.3 (+https://github.com/SanthiagoBR/SolidVision)"
    )
    assert service.timeouts == [TIMEOUT_S]


def test_the_first_result_is_the_place() -> None:
    place = geocoder_for(answering(ANSWER)).geocode(ADDRESS)

    assert place == Place(
        Position(-28.6823, -49.3712),
        "Rua das Palmeiras, Criciúma, Santa Catarina, Brasil",
    )


def test_an_empty_answer_is_no_place() -> None:
    assert geocoder_for(answering([])).geocode(ADDRESS) is None


# --------------------------------------------------------------------------
# One request per second
# --------------------------------------------------------------------------


def test_a_second_request_waits_for_the_rest_of_the_second() -> None:
    clock = FakeClock()
    geocoder = geocoder_for(answering(ANSWER), clock)

    geocoder.geocode("Criciúma")
    clock.now += 0.25
    geocoder.geocode("Joinville")

    assert clock.slept == [pytest.approx(MIN_INTERVAL_S - 0.25)]


def test_a_request_after_the_second_does_not_wait() -> None:
    clock = FakeClock()
    geocoder = geocoder_for(answering(ANSWER), clock)

    geocoder.geocode("Criciúma")
    clock.now += MIN_INTERVAL_S
    geocoder.geocode("Joinville")

    assert clock.slept == []


def test_a_failed_request_still_counts_toward_the_second() -> None:
    clock = FakeClock()
    geocoder = geocoder_for(in_turn(http_error(503), ANSWER), clock)

    with pytest.raises(AddressSearchUnavailableError):
        geocoder.geocode("Criciúma")
    geocoder.geocode("Criciúma")

    assert clock.slept == [pytest.approx(MIN_INTERVAL_S)]


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def test_the_same_address_is_asked_once_whatever_its_case_and_spaces() -> None:
    service = answering(ANSWER)
    geocoder = geocoder_for(service)

    first = geocoder.geocode(ADDRESS)
    second = geocoder.geocode("  rua das palmeiras,  123, CRICIÚMA, sc ")

    assert second == first
    assert len(service.requests) == 1


def test_an_address_that_matched_nothing_is_not_asked_again() -> None:
    service = answering([])
    geocoder = geocoder_for(service)

    geocoder.geocode(ADDRESS)
    geocoder.geocode(ADDRESS)

    assert len(service.requests) == 1


def test_a_failure_is_not_kept() -> None:
    service = in_turn(http_error(503), ANSWER)
    geocoder = geocoder_for(service)

    with pytest.raises(AddressSearchUnavailableError):
        geocoder.geocode(ADDRESS)

    assert geocoder.geocode(ADDRESS) is not None
    assert len(service.requests) == 2


def test_the_oldest_answer_leaves_when_the_cache_is_full() -> None:
    service = answering(ANSWER)
    geocoder = geocoder_for(service)

    for number in range(CACHE_SIZE + 1):
        geocoder.geocode(f"Rua {number}")
    geocoder.geocode(f"Rua {CACHE_SIZE}")
    geocoder.geocode("Rua 0")

    assert len(service.requests) == CACHE_SIZE + 2


# --------------------------------------------------------------------------
# A service that does not answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "reason"),
    [
        (failing(http_error(503)), "HTTP 503"),
        (failing(http_error(429)), "HTTP 429"),
        (failing(urllib.error.URLError(socket.gaierror(11001, "x"))), "gaierror"),
        (failing(urllib.error.URLError(TimeoutError())), "TimeoutError"),
        (failing(TimeoutError()), "TimeoutError"),
        (failing(ConnectionResetError()), "ConnectionResetError"),
        (Service(lambda request: Body(b"<html>")), "unreadable"),
        (answering({"lat": "1"}), "unreadable"),
        (answering([{"display_name": "x"}]), "unreadable"),
        (answering([{"lat": "abc", "lon": "1"}]), "unreadable"),
        (answering([{"lat": "95", "lon": "1"}]), "unreadable"),
    ],
    ids=[
        "503",
        "429",
        "no-network",
        "connect-timeout",
        "read-timeout",
        "reset",
        "not-json",
        "not-a-list",
        "no-coordinates",
        "not-a-number",
        "off-the-planet",
    ],
)
def test_a_service_that_does_not_answer_is_unavailable_with_a_reason(
    service: Service, reason: str
) -> None:
    with pytest.raises(AddressSearchUnavailableError) as raised:
        geocoder_for(service).geocode(ADDRESS)

    assert reason in str(raised.value)


# --------------------------------------------------------------------------
# The address is written nowhere (RFC-026 section 16.1)
# --------------------------------------------------------------------------


def test_no_record_holds_the_address(every_record: pytest.LogCaptureFixture) -> None:
    geocoder = geocoder_for(answering(ANSWER))

    geocoder.geocode(ADDRESS)
    geocoder.geocode(ADDRESS)

    assert every_record.records, "the fixture must be capturing something"
    for record in every_record.records:
        assert "Palmeiras" not in record.getMessage(), record.name


def test_a_failure_holds_the_address_neither_in_its_message_nor_in_its_cause(
    every_record: pytest.LogCaptureFixture,
) -> None:
    """The `HTTPError` the opener raises carries the URL, which is the address."""
    with pytest.raises(AddressSearchUnavailableError) as raised:
        geocoder_for(failing(http_error(500))).geocode(ADDRESS)

    error = raised.value
    assert "Palmeiras" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    for record in every_record.records:
        assert "Palmeiras" not in record.getMessage(), record.name
