"""O aquecimento dos modelos, numa thread, e o que o `/health` diz dele (RFC-036).

A API responde em dois tempos (§5.3). A porta abre assim que a API é
importada, o que não importa mais o `torch`; o aquecimento do RFC-026 §10 roda
depois, numa thread daemon, e é aqui que ele diz como foi.

    search        quando
    loading       o aquecimento está rodando
    ready         terminou, e os dois modelos estão carregados
    unavailable   falhou com `ModelUnavailableError`; o detail é a linha
    on_demand     está desligado, ou falhou por outro motivo: a 1ª busca carrega

O "pronto" do `/health` não depende dele (§5.2): um 200 com `search:
unavailable` é a API do RFC-035 §6.5, que mostra a coleção e não consegue
buscar nela. O que a janela desenha para cada valor é com o frontend.

Aqui, e não em `app.presentation.api`, para que a rota que o lê não importe o
pacote que inclui a rota.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class SearchState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class SearchReadiness:
    state: SearchState
    detail: str | None = None
    """A linha do RFC-035 §6.4, quando `state` é `unavailable`."""


class ModelWarmUp:
    """Roda o aquecimento numa thread daemon, e guarda no que ele deu.

    Daemon, para que um processo que recebe ordem de parar enquanto a thread
    ainda importa `torch` -- 14 a 40 s no disco rígido das medições -- não
    fique preso a ela: a parada do RFC-036 §7.2 dá 15 s à API.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readiness = SearchReadiness(SearchState.ON_DEMAND)
        self._thread: threading.Thread | None = None

    def readiness(self) -> SearchReadiness:
        with self._lock:
            return self._readiness

    def skip(self) -> None:
        """O aquecimento está desligado: a primeira busca carrega os modelos."""
        with self._lock:
            self._readiness = SearchReadiness(SearchState.ON_DEMAND)

    def start(self, warm_up: Callable[[], SearchReadiness]) -> threading.Thread:
        """Roda `warm_up` numa thread, a menos que uma já esteja rodando.

        `warm_up` devolve no que deu, e não deve levantar exceção. Se
        levantar, o estado fica `on_demand` -- o caminho preguiçoso continua
        lá -- e o erro é da thread, informado por `threading.excepthook`.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._readiness = SearchReadiness(SearchState.LOADING)
            self._thread = threading.Thread(
                target=self._run, args=(warm_up,), name="model-warm-up", daemon=True
            )
            self._thread.start()
            return self._thread

    def wait(self, timeout: float | None = None) -> bool:
        """Se o aquecimento terminou, esperando por ele até `timeout`."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _run(self, warm_up: Callable[[], SearchReadiness]) -> None:
        outcome = SearchReadiness(SearchState.ON_DEMAND)
        try:
            outcome = warm_up()
        finally:
            with self._lock:
                self._readiness = outcome


model_warm_up = ModelWarmUp()
"""O aquecimento único do processo, iniciado pelo lifespan da API."""
