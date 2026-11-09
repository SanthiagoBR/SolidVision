"""Os eventos do launcher: um objeto JSON por linha no stdout (RFC-036 §4.4).

    {"event": "starting", "protocol": 1, "pid": 4120, "mode": "installed", ...}
    {"event": "phase", "phase": "starting"}
    {"event": "phase", "phase": "migrating"}
    {"event": "phase", "phase": "api"}
    {"event": "ready", "url": "http://127.0.0.1:51234", "search": "loading", ...}
    {"event": "child", "name": "job_runner", "state": "unavailable", ...}
    {"event": "stopping", "reason": "requested"}
    {"event": "stopped", "exit_code": 0}

O stdout leva esses eventos e nada mais; o texto para pessoas vai para o stderr,
como faz a CLI do RFC-033 §11. Quem iniciou o launcher os lê para saber quando
abrir a janela, e em quê.

    evento           quando, e os seus campos
    starting         primeiro, com `protocol`, `pid`, `mode`; e `database`,
                     `embedded` ou `external`, assim que a configuração foi lida
    phase            `initdb`, `starting`, `recovering`, `migrating` (as fases
                     do cluster), depois `api` e `job_runner`, à medida que
                     cada filho é criado
    ready            uma vez: o `/health` respondeu 200. `url`, `search` (o que
                     o `/health` disse), `indexing` (`running`, `restarting`,
                     `unavailable`)
    already_running  no lugar de todo o resto, quando outro launcher detém a
                     raiz de dados: `protocol`, `pid`, `url`, `state`. Sai com 0
    child            um filho -- `api`, `job_runner` ou o `database` -- está
                     `restarting` (com `exit_code`, `delay`), `running` de novo
                     ou `unavailable` (com `exit_code`), cada um com `detail`,
                     a última linha que ele deixou
    address          a API voltou em outra porta: `url`
    stopping         a parada começou: `reason` é `requested`, `session_end`
                     ou `failure` (com `detail`)
    stopped          a parada terminou: `exit_code`
    failed           o launcher não conseguiu iniciar: `detail`, a linha. Sai
                     com 78

`protocol` muda quando um evento muda de significado; um campo acrescentado não o
muda. Toda linha é ASCII -- `ensure_ascii` -- para que a codificação do pipe não
importe a quem lê.

Nada aqui importa o `Settings` nem o logger: o `already_running` é escrito antes
de a configuração ser lida, por um processo que nunca a lê (§8).
"""

from __future__ import annotations

import json
import threading
from enum import StrEnum
from typing import Protocol, TextIO

PROTOCOL = 1


class Event(StrEnum):
    STARTING = "starting"
    PHASE = "phase"
    READY = "ready"
    ALREADY_RUNNING = "already_running"
    CHILD = "child"
    ADDRESS = "address"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class EventSink(Protocol):
    def emit(self, event: Event, **fields: object) -> None:
        """Escreve um evento, agora."""


class JsonLines:
    """`EventSink` sobre um stream de texto, uma linha por evento, com flush a cada um.

    Thread-safe: a parada de um fim de sessão é pedida a partir da thread de
    uma janela (§7.5). Um leitor que foi embora -- o pipe fechou -- não para o
    launcher: os eventos eram para ele, e a parada que ele com certeza pediu
    ainda tem de acontecer.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, event: Event, **fields: object) -> None:
        line = json.dumps({"event": event.value, **fields}, ensure_ascii=True)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass
