"""Resultado da descoberta no sistema de arquivos de uma imagem candidata."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.position import PositionReading


@dataclass(frozen=True)
class DiscoveredImageFile:
    """Metadados de um arquivo de imagem encontrado por `FilesystemImageProvider`."""

    path: Path
    filename: str
    extension: str
    file_size: int
    file_modified_at: datetime.datetime
    capture_date: CaptureDate | None = None
    """O que o arquivo diz sobre quando foi tirado, lido durante o scan.

    `None` significa *não examinado*: a extração estava desligada
    (`Settings.extract_capture_date`), ou o arquivo não pôde ser lido naquele
    momento. Não é o mesmo que `CaptureDate.unknown()`, que significa que o
    arquivo foi lido e não tem data. Um campo só, e não um par `captured_at` /
    `capture_source`, porque as duas metades são um fato só, e `CaptureDate` é
    onde moram as regras que as amarram.

    Lido aqui, no scan, e não no pipeline de embedding (RFC-028 §6): o scan toca
    todos os arquivos, inclusive os que a checagem incremental vai pular, então
    uma foto já indexada ganha a data de captura sem pagar por inferência.
    """

    position: PositionReading | None = None
    """O que o arquivo diz sobre onde foi tirado, lido na mesma abertura (RFC-032).

    A mesma semântica de `None` que em `capture_date`: *não examinado* --
    `Settings.extract_gps` está desligado, ou o arquivo não pôde ser lido --, o
    que não é `PositionReading.unknown()`, o arquivo lido e encontrado sem uma
    posição utilizável. Os dois são preenchidos por uma chamada a
    `read_exif_facts()`, então um arquivo custa uma abertura de cabeçalho
    qualquer que seja o ligado, e eles continuam independentes: qualquer um pode
    ser `None` ou `unknown` enquanto o outro tem um valor.
    """
