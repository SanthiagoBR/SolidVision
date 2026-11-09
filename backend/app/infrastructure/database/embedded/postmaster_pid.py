"""`postmaster.pid`, lido como o servidor o escreveu (RFC-033 §8.2 e §9).

A trava do próprio servidor sobre o diretório de dados, e a única fonte de
verdade sobre um cluster rodando: a linha 1 é o PID, a linha 2 o diretório de
dados, a linha 4 a porta que o servidor está *usando* -- o `solidvision.conf`
só diz qual o adaptador *pediu* -- e a linha 8 o estado. Um arquivo de estado
do próprio adaptador seria uma terceira fonte, capaz de discordar das duas.

Somente leitura. O adaptador nunca apaga este arquivo (§9.2): com um servidor
vivo, apagá-lo deixa um segundo postmaster subir no mesmo `data\\`, e dois
postmasters num diretório corrompem o cluster. Se uma trava deixada para trás
ainda vale é decisão do servidor, não deste módulo.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

STATUS_STARTING = "starting"
STATUS_READY = "ready"
STATUS_STOPPING = "stopping"
STATUS_STANDBY = "standby"


@dataclass(frozen=True)
class PostmasterPid:
    """As linhas do `postmaster.pid` com que o adaptador age."""

    pid: int
    data_directory: str
    port: int | None
    status: str | None
    """`starting`, `ready`, `stopping` ou `standby`; `None` até a linha 8 existir.

    O servidor escreve o arquivo em etapas enquanto sobe, então um arquivo lido
    nos primeiros milissegundos pode ter um PID e nada depois dele.
    """

    def points_to(self, data: Path) -> bool:
        """Se a linha 2 nomeia este diretório de dados (§8.4).

        O servidor canonicaliza o caminho com barras normais, e a caixa pode
        diferir da que o adaptador passou; o `samefile` dá a palavra final
        quando as duas grafias existem.
        """
        if not self.data_directory:
            return False
        recorded = os.path.normcase(os.path.normpath(self.data_directory))
        expected = os.path.normcase(os.path.normpath(str(data)))
        if recorded == expected:
            return True
        try:
            return os.path.samefile(self.data_directory, data)
        except OSError:
            return False


def read_postmaster_pid(path: Path) -> PostmasterPid | None:
    """Lê o `postmaster.pid`, ou `None` quando ele não existe ou ainda não tem PID."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    # O servidor escreve caminhos na code page ANSI, como todo programa de
    # `main` estreito no Windows (§10).
    encoding = "mbcs" if sys.platform == "win32" else "utf-8"
    lines = [line.rstrip("\r") for line in raw.decode(encoding, "replace").split("\n")]
    if not lines or not lines[0].strip().isdigit():
        return None

    def line(number: int) -> str:
        return lines[number - 1].strip() if len(lines) >= number else ""

    port = line(4)
    return PostmasterPid(
        pid=int(lines[0].strip()),
        data_directory=line(2),
        port=int(port) if port.isdigit() else None,
        status=line(8) or None,
    )
