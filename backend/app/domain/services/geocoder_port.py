"""Abstract contract for turning a typed address into a place (RFC-043)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.place import Place


class GeocoderPort(ABC):
    @abstractmethod
    def geocode(self, address: str) -> Place | None:
        """The best match, or `None` when nothing matched -- which is not an error.

        A service that cannot answer raises its adapter's own exception, as a
        model that cannot load raises `ModelUnavailableError`.
        """
