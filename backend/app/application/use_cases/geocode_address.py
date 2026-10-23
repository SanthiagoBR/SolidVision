"""Find a typed address on the map (RFC-043): address -> GeocoderPort -> Place."""

from __future__ import annotations

from app.domain.exceptions import EmptyAddressError
from app.domain.services.geocoder_port import GeocoderPort
from app.domain.value_objects.place import Place


class GeocodeAddressUseCase:
    def __init__(self, geocoder: GeocoderPort) -> None:
        self._geocoder = geocoder

    def execute(self, address: str) -> Place | None:
        text = address.strip()
        if not text:
            # Refused before anything leaves the machine.
            raise EmptyAddressError()
        return self._geocoder.geocode(text)
