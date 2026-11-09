"""Os dois fatos gravados que dizem se um arquivo pode ter mudado (RFC-045)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChangeSignals:
    """O tamanho e a data de modificação que uma linha gravou do arquivo dela.

    O que `ImageRepository.list_change_signals()` devolve para cada linha de
    um dispositivo: o instantâneo que a thread de leitura antecipada consulta
    para prever, antes de ler um único byte, se a decisão incremental vai
    pedir os bytes do arquivo (RFC-045 §5.2). A comparação em si é
    `is_unchanged()`, ao lado de `plan_indexing()`, para que a regra fique
    num lugar só.

    **Não é `IndexMetadata`.** Aquele traz também o hash de conteúdo, as
    fontes da data de captura e da posição e o local da thumbnail, porque a
    janela de prefetch precisa deles; um instantâneo de um dispositivo
    inteiro precisa de dois escalares por linha, e guarda 116.396 deles de
    uma vez no maior disco do usuário. `slots` pela mesma razão: um objeto
    por linha.

    Os dois campos podem ser `None`, para uma linha escrita antes do RFC-020
    que nunca os teve. `is_unchanged()` trata isso como mudado, exatamente
    como `plan_indexing()` faz.
    """

    file_size: int | None
    file_modified_at: datetime.datetime | None
