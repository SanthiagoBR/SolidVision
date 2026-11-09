"""Uma data de captura examinada: o valor e de onde ele veio (RFC-028).

`captured_at` e `capture_source` são duas colunas e dois campos de `Image`,
mas são um fato só, e as regras que os amarram são do tipo que quebra em
silêncio quando cada metade é escrita por conta própria: uma data sem fonte,
uma fonte que diz EXIF sem data, ou uma data com um fuso horário que o
arquivo nunca gravou. Este value object é onde essas regras são aplicadas
uma vez, e é o que a extração produz e o que a porta do repositório aceita
quando escreve uma data sem tocar em mais nada.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.exceptions import InvalidCaptureDateError
from app.domain.value_objects.capture_source import CaptureSource


@dataclass(frozen=True)
class CaptureDate:
    """O resultado de examinar um arquivo para saber quando ele foi tirado.

    Sempre o resultado de um exame. "Nunca examinada" não é um `CaptureDate`
    -- é a *ausência* de um, `None` onde se espera um `CaptureDate | None` --
    e essa assimetria é a distinção entre NULL e `'unknown'` do RFC-028 §4.2
    expressa como tipo.
    """

    captured_at: datetime.datetime | None
    """A hora local do relógio da câmera, sem fuso horário. Nunca aware.

    O EXIF `DateTimeOriginal` é `YYYY:MM:DD HH:MM:SS` e mais nada: uma
    leitura do relógio da câmera, e não um instante. Anexar qualquer fuso --
    UTC, ou o fuso da máquina que por acaso rodou o scan -- inventa uma
    informação que o arquivo não contém e leva as fotos da virada do ano para
    o outro lado da fronteira entre os anos (RFC-028 §5).
    """

    source: CaptureSource

    def __post_init__(self) -> None:
        validate_capture_fields(self.captured_at, self.source)

    @classmethod
    def unknown(cls) -> CaptureDate:
        """O arquivo foi examinado e não tem data utilizável."""
        return cls(captured_at=None, source=CaptureSource.UNKNOWN)


def validate_capture_fields(
    captured_at: datetime.datetime | None, source: CaptureSource | None
) -> None:
    """Aplica as regras de pareamento de uma data de captura guardada em dois campos.

    Compartilhada por `CaptureDate` e `Image`, que guarda as duas metades
    separadas porque são duas colunas. `source is None` é válido aqui e em
    nenhum lugar de `CaptureDate`: em `Image`, quer dizer que a linha nunca
    foi examinada, e então também não pode haver data.

    A verificação do fuso horário é a que tem dentes. Um valor aware
    comparado com uma coluna `TIMESTAMP WITHOUT TIME ZONE` falha dentro do
    PostgreSQL, e comparado com um valor naive no Python levanta `TypeError`
    -- duas falhas diferentes para a mesma chamada, que é o que o teste de
    contrato compartilhado do repositório existe para descartar. Recusá-lo na
    construção torna as duas impossíveis.
    """
    if captured_at is not None and captured_at.tzinfo is not None:
        raise InvalidCaptureDateError(
            f"A capture date is the camera's local time and carries no time "
            f"zone; got {captured_at.isoformat()}."
        )
    if source is None:
        if captured_at is not None:
            raise InvalidCaptureDateError(
                "A capture date needs the source it was read from."
            )
        return
    if not isinstance(source, CaptureSource):
        raise InvalidCaptureDateError("Capture source must be a CaptureSource member.")
    if (captured_at is None) != (source is CaptureSource.UNKNOWN):
        raise InvalidCaptureDateError(
            f"Capture source {source.value!r} does not match "
            f"{'a missing' if captured_at is None else 'a present'} date: "
            f"'unknown' is exactly the case with no date."
        )
