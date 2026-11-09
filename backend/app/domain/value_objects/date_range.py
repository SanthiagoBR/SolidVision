"""Um intervalo semiaberto de horas de captura no relógio da câmera (RFC-028 §8)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.exceptions import InvalidDateRangeError


@dataclass(frozen=True)
class DateRange:
    """`[start, end)`: `start` está incluído, `end` não.

    Semiaberto porque "2018" é `[2018-01-01, 2019-01-01)`, que não precisa
    saber quantos segundos tem o último dia. A forma fechada, `<= 2018-12-31`,
    descarta em silêncio tudo o que foi fotografado depois da meia-noite do
    réveillon -- e `<= 2018-12-31T23:59:59` descarta o que cair dentro do
    último segundo. Intervalos semiabertos consecutivos também se encaixam
    sem sobreposição, então uma foto nunca pode ser contada em dois anos
    vizinhos.

    **Os dois limites são naive e precisam continuar naive.** Eles são
    comparados com `images.captured_at`, um `TIMESTAMP WITHOUT TIME ZONE`
    com o relógio local da câmera (RFC-028 §5). Um limite aware falha contra
    essa coluna no PostgreSQL e levanta `TypeError` contra um valor naive no
    Python: duas falhas diferentes para uma chamada, exatamente o que o
    contrato compartilhado do repositório existe para proibir. Então ele
    falha aqui, igual em todo lugar, e nunca é convertido "para ajudar" --
    *para qual* fuso converter é a pergunta que a coluna de propósito não
    responde.

    **Os dois limites são obrigatórios.** Uma ponta aberta é assunto da
    Presentation: a camada HTTP transforma um `captured_to` omitido em
    `datetime.max` antes de montar este objeto, para que todo consumidor
    daqui leia dois datetimes, em vez de cada um tratar um `None` próprio. O
    caso em que *nenhum* limite foi dado também não é um `DateRange` sem
    limites; é `SearchFilters.captured_between = None`, que não acrescenta
    cláusula nenhuma.

    `start == end` é válido e não casa com nada. É o sentido honesto do
    pedido, e recusá-lo faria um cliente que calcula intervalos
    aritmeticamente tratar o vazio como caso especial.
    """

    start: datetime.datetime
    end: datetime.datetime

    def __post_init__(self) -> None:
        for name, bound in (("start", self.start), ("end", self.end)):
            if not isinstance(bound, datetime.datetime):
                raise InvalidDateRangeError(f"Date range {name} must be a datetime.")
            if bound.tzinfo is not None:
                raise InvalidDateRangeError(
                    f"Date range {name} carries a time zone ({bound.isoformat()}); "
                    "capture dates are camera-local times with no zone, so a "
                    "range over them must not have one either."
                )
        if self.start > self.end:
            raise InvalidDateRangeError(
                f"Date range start {self.start.isoformat()} is after its end "
                f"{self.end.isoformat()}."
            )

    def contains(self, moment: datetime.datetime) -> bool:
        """Diz se `moment` cai em `[start, end)`.

        Recebe um `datetime`, nunca `None`. Se uma data de captura
        desconhecida "está dentro" de um intervalo não é uma pergunta que
        este objeto responde: a regra do RFC-020 é que desconhecido nunca
        casa, e todo chamador precisa dizer isso explicitamente, em vez de
        herdá-lo de um padrão daqui.
        """
        return self.start <= moment < self.end
