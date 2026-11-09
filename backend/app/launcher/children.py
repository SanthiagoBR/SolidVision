r"""Um filho: como é criado, lido, chamado a parar e encerrado (RFC-036 §5.5, §7).

    <python> -X utf8 -m app.presentation.api --port 51234 --supervised
    <python> -X utf8 -m app.infrastructure.workers.job_runner --supervised

`<python>` é o próprio interpretador do launcher, e `child_command()` é a única
função a mudar quando o runtime for congelado, como `detect_layout()` é para a
raiz (RFC-034 §4.1).

* **`CREATE_NO_WINDOW`.** Cada filho recebe um console que ninguém vê: nenhuma
  janela abre quando o launcher não tem console, e um Ctrl+C no terminal do
  desenvolvedor chega só ao launcher, que ordena a parada (§7.2).
* **O stdin é um pipe que só o launcher segura, e no qual nunca escreve.** Ele
  existe para ser fechado: esse é o pedido de parada (§7.1). Quando o launcher
  morre, o Windows o fecha, e o pedido chega do mesmo jeito (§7.4).
* **stdout e stderr são pipes**, cada um lido por uma thread que nunca para de
  ler -- um pipe que ninguém esvazia enche, e bloqueia o filho na próxima
  escrita. Cada linha vai para o stderr do launcher com o nome do filho na
  frente, e as últimas 50 ficam em memória, para o evento `child` e para o log
  do launcher quando o filho morre. Os filhos continuam escrevendo os próprios
  logs (RFC-034 §8.2).
* **`-X utf8`.** Por um pipe, o Python escreve na code page ANSI, e um caminho
  fora dela numa linha de log vira um erro de codificação no handler do
  console; no modo UTF-8 o pipe carrega tudo, decodificado aqui como UTF-8.
* **Ninguém herda esses pipes.** O `subprocess` entrega a um filho só os
  handles que recebe (`close_fds`, o padrão desde o Python 3.7 no Windows), e
  nenhum dos filhos inicia um processo de vida longa próprio: o único da API é
  o Explorer, para o "mostrar", com os três handles em `DEVNULL`. Então os dois
  leitores terminam quando o filho termina (§16 verifica isso).
* **O ambiente é o do launcher, sem nada acrescentado.** Os filhos calculam o
  mesmo layout e leem o mesmo arquivo (RFC-034 §5.2).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, Protocol, TextIO

CREATE_NO_WINDOW = 0x08000000

TAIL_LINES = 50

DRAIN_SECONDS = 2.0
"""Quanto tempo os leitores de um filho que saiu têm para entregar as últimas linhas."""

TERMINATE_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class Program:
    """Um filho, pelo nome que os eventos usam e pelo módulo que o `-m` roda."""

    name: str
    module: str


API = Program("api", "app.presentation.api")
JOB_RUNNER = Program("job_runner", "app.infrastructure.workers.job_runner")


def child_command(program: Program, arguments: Sequence[str]) -> list[str]:
    """A linha de comando de um filho, montada num lugar só."""
    return [sys.executable, "-X", "utf8", "-m", program.module, *arguments]


def last_line(lines: Sequence[str]) -> str | None:
    """A última linha que diz alguma coisa: o `detail` de um evento `child`."""
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return None


class Child(Protocol):
    """O que o supervisor precisa de um filho; os testes põem um dublê no lugar."""

    @property
    def name(self) -> str: ...

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None:
        """O código de saída, ou `None` enquanto ele roda."""

    def request_stop(self) -> None:
        """Fecha o stdin dele: o Ctrl+C de §7.1."""

    def terminate(self) -> None:
        """`TerminateProcess`, e espera que ele suma."""

    def drain(self, timeout: float = DRAIN_SECONDS) -> bool:
        """Espera os leitores de um filho que saiu chegarem ao fim."""

    def last_lines(self) -> tuple[str, ...]:
        """As últimas linhas que ele escreveu, da mais antiga para a mais nova."""


class StderrEcho:
    """Para onde vão as linhas dos filhos: o stderr do launcher, uma linha por vez.

    Um lock para todos os leitores, para que as linhas de dois filhos nunca se
    intercalem. Um stderr que sumiu -- a janela fechou o pipe -- é ignorado.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def __call__(self, line: str) -> None:
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass


class ChildProcess:
    """Um processo filho real, com os seus dois leitores."""

    def __init__(
        self,
        name: str,
        process: subprocess.Popen[bytes],
        echo: Callable[[str], None],
    ) -> None:
        self._name = name
        self._process = process
        self._echo = echo
        self._lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._lines_lock = threading.Lock()
        self._readers = [
            self._read_in_thread(process.stdout),
            self._read_in_thread(process.stderr),
        ]

    @classmethod
    def start(
        cls, name: str, command: Sequence[str], *, echo: Callable[[str], None]
    ) -> ChildProcess:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        return cls(name, process, echo)

    @property
    def name(self) -> str:
        return self._name

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def request_stop(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass

    def terminate(self) -> None:
        if self._process.poll() is None:
            try:
                self._process.kill()
            except OSError:
                pass
        try:
            self._process.wait(TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        self.request_stop()

    def drain(self, timeout: float = DRAIN_SECONDS) -> bool:
        for reader in self._readers:
            reader.join(timeout)
        return not any(reader.is_alive() for reader in self._readers)

    def last_lines(self) -> tuple[str, ...]:
        with self._lines_lock:
            return tuple(self._lines)

    def _read_in_thread(self, stream: IO[bytes] | None) -> threading.Thread:
        reader = threading.Thread(
            target=self._read,
            args=(stream,),
            name=f"{self._name}-output",
            daemon=True,
        )
        reader.start()
        return reader

    def _read(self, stream: IO[bytes] | None) -> None:
        if stream is None:
            return
        with stream:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self._lines_lock:
                    self._lines.append(line)
                self._echo(f"{self._name} | {line}")
