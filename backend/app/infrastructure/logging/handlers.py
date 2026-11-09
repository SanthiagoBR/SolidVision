"""Implementações de handler da infraestrutura de logging."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .formatters import AppFormatter

ROLLOVER_RETRY_SECONDS = 1.0
"""Quanto uma rotação que não pôde acontecer espera antes de ser tentada de novo.

Medido (RFC-034 §16): uma tentativa que falha -- fechar, o rename que o Windows
recusa, reabrir -- custa 0,19 ms no disco rígido da medição, contra 0,01 ms de
um registro. Tentada a cada registro, deixou 600 registros dez vezes mais
lentos. Uma vez por segundo, é 0,02 % do tempo, e o critério declarado antes de
medir era o menor de 1, 5 e 30 s abaixo de 0,1 %: daí em diante, menor é
melhor, porque é o tempo que um arquivo fica acima do limite depois que o outro
processo foi embora.
"""


class LosslessRotatingFileHandler(RotatingFileHandler):
    """Uma rotação que não perde nem o registro nem um backup (RFC-034 §8.3).

    O Windows se recusa a renomear um arquivo que outro processo mantém aberto,
    e o `open()` do Python não pede para compartilhar isso. O handler padrão
    desloca os backups *antes* de tentar o rename que falha, e depois descarta
    o registro que o disparou: dois processos num arquivo perderam 381 de 600
    registros e quatro de cinco backups (medido, §2.6).

    Aqui a ordem é a inversa:

    1. O rename que pode falhar vem primeiro -- o do arquivo atual.
    2. Se falhar, o arquivo é reaberto, **o registro é escrito**, e a próxima
       tentativa é adiada por `ROLLOVER_RETRY_SECONDS`. Nenhum backup se mexeu.
    3. Só com esse rename feito os backups são deslocados.

    O custo declarado: enquanto dois processos vivos compartilham um arquivo,
    ele não roda, e cresce além do limite. A primeira rotação depois que um
    deles sai o corta. Ele cresce em vez de perder linhas.
    """

    _next_attempt = 0.0

    @property
    def _outgoing(self) -> str:
        """Onde o arquivo atual espera enquanto os backups são deslocados."""
        return f"{self.baseFilename}.rotating"

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        if self.backupCount <= 0 or time.monotonic() < self._next_attempt:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            # Uma rotação interrompida entre os seus dois passos deixou isto.
            if os.path.exists(self._outgoing):
                self._shelve()
            os.rename(self.baseFilename, self._outgoing)
            self._shelve()
        except OSError:
            self._next_attempt = time.monotonic() + ROLLOVER_RETRY_SECONDS
        self.stream = self._open()

    def _shelve(self) -> None:
        """Desloca `.1` para `.2` e assim por diante; o arquivo de saída vira o `.1`."""
        for number in range(self.backupCount - 1, 0, -1):
            source = f"{self.baseFilename}.{number}"
            if os.path.exists(source):
                os.replace(source, f"{self.baseFilename}.{number + 1}")
        os.replace(self._outgoing, f"{self.baseFilename}.1")


def build_console_handler() -> logging.Handler:
    """Cria um handler de console."""
    handler = logging.StreamHandler()
    handler.setFormatter(AppFormatter())
    return handler


def build_rotating_file_handler(
    log_directory: Path, log_filename: str
) -> logging.Handler:
    """Cria um handler de arquivo com rotação."""
    log_directory.mkdir(parents=True, exist_ok=True)
    file_path = log_directory / log_filename
    handler = LosslessRotatingFileHandler(
        filename=file_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(AppFormatter())
    return handler
