"""Toda foto sob os filtros, em ordem de data de captura (RFC-044 §5.4).

Uma listagem não é uma busca. Ela não tem consulta e, portanto, não tem
nota: `SearchHit` traz uma `similarity` obrigatória de propósito (RFC-025
§4.1), e uma listagem que a preenchesse com um número inventado devolveria
fotos como se tivessem sido comparadas. A resposta são `Image`s simples, na
ordem de `ListingOrder`, uma página por vez.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum

from app.domain.entities.image import Image


class ListingOrder(StrEnum):
    """Para que lado corre a data de captura. Os desempates nunca mudam de sentido.

    `NEWEST` responde primeiro "como está agora", a convenção de toda
    galeria; `OLDEST` é a mesma lista com a data invertida -- "do buraco no
    chão ao prédio pronto" (RFC-044 §3.1).

    Nos dois, uma foto sem data de captura vem **depois de toda foto com
    data**, na ordem da pasta (§3.2). Ela não tem lugar numa linha do tempo, e
    pô-la primeiro num dos sentidos poria as sem data no topo da lista.
    """

    NEWEST = "newest"
    OLDEST = "oldest"


@dataclass(frozen=True)
class ListingSummary:
    """A lista inteira num relance, para o cabeçalho da primeira página (§5.3).

    `total` conta toda foto da lista, as sem data incluídas; `undated` é
    quantas delas não têm data de captura -- o grupo abaixo do divisor.
    `oldest` e `newest` são a primeira e a última datas de captura, naive e no
    horário local da câmera como `Image.captured_at` (RFC-028 §5), e `None`
    quando nenhuma foto da lista tem data.
    """

    total: int
    undated: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None


# O alias pela razão de `SearchHits` existir: dentro do corpo de uma classe
# `ImageRepository`, `list[Image]` nomearia o método `list()`, e não o tipo.
ListedImages = list[Image]
