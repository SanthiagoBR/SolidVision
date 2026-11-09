"""Endereços achados pelo Nominatim, o geocodificador do OpenStreetMap (RFC-043).

A política de uso (https://operations.osmfoundation.org/policies/nominatim/,
lida em 22/10/2026) pede no máximo uma requisição por segundo, um User-Agent
que nomeie a aplicação, cache no cliente, um serviço que possa ser trocado sem
atualizar o software (`ADDRESS_SEARCH_URL`) e nenhum autocompletar. O endereço
nunca é escrito em lugar nenhum: nem no log, nem em disco, nem num erro
(RFC-026 §16.1).

`urllib`, não `httpx`: a API já o importa, e o `httpx` acrescentava de 146 a
267 ms ao import da API.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol

from app.domain.exceptions import InvalidPositionError
from app.domain.services.geocoder_port import GeocoderPort
from app.domain.value_objects.place import Place
from app.domain.value_objects.position import Position
from app.infrastructure.logging.logger import get_logger

PROJECT_URL = "https://github.com/SanthiagoBR/SolidVision"
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 10.0
CACHE_SIZE = 256
# O mapa é o do Brasil, e "Rua das Palmeiras, 123" existe em uma dúzia de países.
COUNTRY_CODES = "br"

logger = get_logger(__name__)


class Opener(Protocol):
    def __call__(
        self, request: urllib.request.Request, /, *, timeout: float
    ) -> Any: ...


class AddressSearchUnavailableError(Exception):
    """Não deu para consultar o serviço, ou não deu para ler a resposta: um 503."""


class NominatimGeocoder(GeocoderPort):
    def __init__(
        self,
        url: str,
        version: str,
        *,
        opener: Opener = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self._user_agent = f"SolidVision/{version} (+{PROJECT_URL})"
        self._open = opener
        self._clock = clock
        self._sleep = sleep
        # Mantido também durante a requisição, para que duas consultas nunca
        # dividam o mesmo segundo.
        self._lock = threading.Lock()
        self._last_request_at: float | None = None
        self._cache: OrderedDict[str, Place | None] = OrderedDict()

    def geocode(self, address: str) -> Place | None:
        text = " ".join(address.split())
        key = text.casefold()
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                logger.info("address search answered from the cache")
                return self._cache[key]
            waited_s = self._wait_for_the_second()
            started_at = self._clock()
            self._last_request_at = started_at
            place = self._ask(text)
            self._cache[key] = place
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        logger.info(
            "address search completed: matched=%s, waited_ms=%.0f, elapsed_ms=%.0f",
            place is not None,
            waited_s * 1000,
            (self._clock() - started_at) * 1000,
        )
        return place

    def _wait_for_the_second(self) -> float:
        if self._last_request_at is None:
            return 0.0
        remaining = MIN_INTERVAL_S - (self._clock() - self._last_request_at)
        if remaining <= 0:
            return 0.0
        self._sleep(remaining)
        return remaining

    def _ask(self, text: str) -> Place | None:
        query = urllib.parse.urlencode(
            {
                "q": text,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": COUNTRY_CODES,
                "accept-language": "pt-BR",
            }
        )
        request = urllib.request.Request(
            f"{self._url}?{query}",
            headers={"User-Agent": self._user_agent, "Accept": "application/json"},
        )
        try:
            with self._open(request, timeout=TIMEOUT_S) as response:
                body = response.read()
            return _first_place(json.loads(body))
        except urllib.error.HTTPError as error:
            error.close()
            reason = f"HTTP {error.code}"
        except urllib.error.URLError as error:
            cause = error.reason
            reason = type(cause if isinstance(cause, BaseException) else error).__name__
        except (OSError, http.client.HTTPException) as error:
            reason = type(error).__name__
        except (ValueError, KeyError, TypeError, InvalidPositionError):
            reason = "an unreadable answer"
        logger.warning("address search failed: %s", reason)
        # `from None`: o erro encadeado levaria a URL, que é o endereço.
        raise AddressSearchUnavailableError(
            f"The address search service did not answer ({reason})."
        ) from None


def _first_place(results: Any) -> Place | None:
    if not isinstance(results, list):
        raise TypeError("not a list")
    if not results:
        return None
    first = results[0]
    position = Position(float(first["lat"]), float(first["lon"]))
    return Place(position=position, label=str(first.get("display_name") or ""))
