"""Contrato abstrato para transformar um endereço digitado num lugar (RFC-043)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.place import Place


class GeocoderPort(ABC):
    @abstractmethod
    def geocode(self, address: str) -> Place | None:
        """O melhor resultado, ou `None` quando nada casou -- o que não é erro.

        Um serviço que não consegue responder levanta a exceção do próprio
        adaptador, como um modelo que não consegue carregar levanta
        `ModelUnavailableError`.
        """
