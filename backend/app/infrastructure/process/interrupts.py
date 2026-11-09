"""Interrupções que esperam a chamada ao banco em andamento (RFC-036 §7.5).

O job runner é parado por um Ctrl+C -- o do launcher, pelo fim do stdin
(§7.1) -- e o `KeyboardInterrupt` cai onde quer que a thread principal esteja.
Dentro de uma chamada ao banco, ele custa uma conexão nova, de um de dois
jeitos:

* o psycopg, interrompido enquanto uma consulta roda, manda ao servidor um
  pedido de cancelamento (`Connection.wait`, psycopg 3.3), e um pedido de
  cancelamento é uma conexão; ou
* o SQLAlchemy invalida uma conexão por onde passou uma interrupção, e a
  próxima chamada -- o `release()` do job -- abre outra.

O PostgreSQL no Windows atende cada conexão num processo novo, e durante um
logoff nenhum processo novo inicia: ele morre com `0xC000026B`
(`STATUS_DLL_INIT_FAILED_LOGOFF`), o postmaster toma isso por uma queda e
encerra todos os outros processos do servidor, também não consegue iniciar o
processo de startup, e desliga -- com o job ainda `running`, para o reaper.
Medido em dois logoffs de três, em 20/10/2026.

Então, num processo que chamou `install()`, uma interrupção que chega enquanto
a thread principal está dentro de `deferred()` espera o bloco terminar, e é
levantada ali: depois que a chamada voltou, fora do psycopg e do SQLAlchemy,
com a conexão inteira e livre para o `release()`. Em qualquer outro lugar ela é
levantada na hora, como antes -- no import do `torch`, na codificação de um
lote, na leitura de um arquivo --, então uma parada não demora mais do que
demorava. `deferring()` põe cada método de um repositório dentro de
`deferred()`.

Só a thread principal: o Python roda os handlers de sinal ali, e só ali uma
interrupção pode cair dentro de uma chamada. Só biblioteca padrão, como o
`supervision`.
"""

from __future__ import annotations

import functools
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any, cast

_thread = threading.local()
"""`depth` e `pending`, por thread. O handler roda na thread principal, e por
isso lê os da thread principal."""


def install() -> None:
    """Faz o SIGINT -- um Ctrl+C, ou o da vigia do stdin -- esperar `deferred()`."""
    signal.signal(signal.SIGINT, _interrupt)


@contextmanager
def deferred() -> Iterator[None]:
    """Um bloco em que nenhuma interrupção cai: a que chega é levantada no fim dele.

    Blocos aninhados levantam no fim do mais externo. Levantada enquanto outra
    exceção sai do bloco, a interrupção vence, com aquela exceção como
    contexto: é o que o operador pediu.
    """
    _thread.depth = _depth() + 1
    try:
        yield
    finally:
        _thread.depth -= 1
        if _thread.depth == 0 and getattr(_thread, "pending", False):
            _thread.pending = False
            raise KeyboardInterrupt


def deferring[T](target: T) -> T:
    """`target`, com cada um dos seus métodos chamado dentro de `deferred()`."""
    return cast(T, _Deferring(target))


class _Deferring:
    def __init__(self, target: object) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if not callable(attribute):
            return attribute
        return _deferred_call(attribute)

    def __repr__(self) -> str:
        return f"deferring({self._target!r})"


def _deferred_call(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        with deferred():
            return method(*args, **kwargs)

    return call


def _depth() -> int:
    return int(getattr(_thread, "depth", 0))


def _interrupt(signum: int, frame: FrameType | None) -> None:
    if _depth():
        _thread.pending = True
        return
    raise KeyboardInterrupt
