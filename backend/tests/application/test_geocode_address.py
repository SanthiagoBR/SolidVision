"""`GeocodeAddressUseCase` (RFC-043): the request policy, over a recording port."""

from __future__ import annotations

import pytest

from app.application.use_cases.geocode_address import GeocodeAddressUseCase
from app.domain.exceptions import EmptyAddressError
from app.domain.services.geocoder_port import GeocoderPort
from app.domain.value_objects.place import Place
from app.domain.value_objects.position import Position

CRICIUMA = Place(Position(-28.6775, -49.3697), "Criciúma, Santa Catarina, Brasil")


class RecordingGeocoder(GeocoderPort):
    def __init__(self, answer: Place | None) -> None:
        self.asked: list[str] = []
        self._answer = answer

    def geocode(self, address: str) -> Place | None:
        self.asked.append(address)
        return self._answer


def test_the_address_is_sent_without_the_spaces_around_it() -> None:
    geocoder = RecordingGeocoder(CRICIUMA)

    place = GeocodeAddressUseCase(geocoder).execute("  Criciúma, SC \n")

    assert place == CRICIUMA
    assert geocoder.asked == ["Criciúma, SC"]


def test_an_address_that_matches_nothing_is_none_and_not_an_error() -> None:
    geocoder = RecordingGeocoder(None)

    assert GeocodeAddressUseCase(geocoder).execute("Rua que não existe") is None


@pytest.mark.parametrize("address", ["", "   ", "\t\n"])
def test_an_empty_address_is_refused_before_the_service_is_asked(
    address: str,
) -> None:
    geocoder = RecordingGeocoder(CRICIUMA)

    with pytest.raises(EmptyAddressError):
        GeocodeAddressUseCase(geocoder).execute(address)

    assert geocoder.asked == []
