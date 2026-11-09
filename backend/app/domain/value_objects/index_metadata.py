"""Portador dos metadados de sistema de arquivos gravados anteriormente."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.position_source import PositionSource


@dataclass(frozen=True)
class IndexMetadata:
    """Metadados de sistema de arquivos relidos da persistência para comparar mudanças.

    Deixa de fora `embedding` e `Image` de propósito -- reconstruir um vetor
    de embedding inteiro só para comparar dois escalares seria desperdício,
    e nenhuma reconstrução assim existe em outro lugar do código. Mora no
    Domain porque faz parte do contrato da porta `ImageRepository`.
    """

    file_size: int | None
    file_modified_at: datetime.datetime | None
    content_hash: str | None = None
    """Impressão digital dos bytes do arquivo na última vez que ele foi indexado.

    O padrão é `None`, que quer dizer *desconhecido*, nunca *casa*. Linhas
    escritas antes do RFC-024 têm NULL aqui, e a coluna foi acrescentada sem
    backfill, então um chamador que compara hashes precisa tratar `None` como
    "não dá para confirmar que não mudou" e seguir para recalcular o
    embedding. Com padrão, e não obrigatório, para que todo ponto de
    construção anterior ao hash de conteúdo continue produzindo a resposta
    segura, em vez de afirmar em silêncio uma igualdade que nunca verificou.
    """

    capture_source: CaptureSource | None = None
    """Se a data de captura desta linha já foi examinada, e de onde veio.

    **NÃO É UM SINAL DE MUDANÇA. `plan_indexing()` nunca pode lê-lo.** Ele
    pega carona no prefetch para outro consumidor: o scan escreve uma data de
    captura só para as linhas cuja fonte ainda é `None` -- nunca examinadas
    -- para que um novo scan de uma coleção inalterada custe zero escritas
    depois da primeira passada (RFC-028 §6). O prefetch já é uma consulta
    por janela, então mais um escalar nele sai de graça, enquanto uma busca
    separada seria a ida e volta por arquivo que o RFC-024 §7.1 eliminou.

    Uma data de captura diferente não diz nada sobre se os pixels mudaram.
    Compará-la na decisão de pular recalcularia o embedding de uma foto -- a
    operação mais cara do sistema -- porque o EXIF dela foi editado ou a
    extração melhorou (RFC-028 §6.1). `test_indexing_plan.py` falha se essa
    comparação for acrescentada algum dia.

    Só `ImageRepository.update_capture_date*()` escreve esta coluna;
    `update_index_metadata()` a deixa intocada, embora receba um
    `IndexMetadata`.
    """

    position_source: PositionSource | None = None
    """Se a posição desta linha já foi examinada, e de onde veio (RFC-032).

    **NÃO É UM SINAL DE MUDANÇA. `plan_indexing()` nunca pode lê-lo.** Ele
    pega carona no prefetch pela razão de `capture_source`: o scan escreve
    uma posição só para as linhas cuja fonte ainda é `None` -- nunca
    examinadas -- para que um novo scan de uma coleção inalterada custe zero
    escritas depois da primeira passada (RFC-032 §8).

    Uma coordenada lida hoje não diz nada sobre se os pixels mudaram.
    Compará-la na decisão de pular recalcularia o embedding de uma foto -- a
    operação mais cara do sistema -- por um fato de metadado, e
    `test_indexing_plan.py` falha, verificado por mutação, se essa comparação
    for acrescentada algum dia.

    Só `ImageRepository.update_position*()` e `save_indexed*()` escrevem esta
    coluna; `update_index_metadata()` a deixa intocada, embora receba um
    `IndexMetadata`, porque uma atualização que a escrevesse voltaria uma
    linha examinada para "nunca examinada".
    """

    thumbnail_path: str | None = None
    """Onde a thumbnail desta linha está guardada, ou `None` se não há (RFC-030).

    **NÃO É UM SINAL DE MUDANÇA. `plan_indexing()` nunca pode lê-lo.** Ele
    pega carona no prefetch pela mesma razão de `capture_source`: o backfill
    de thumbnails pergunta "quais destas linhas já têm uma?" uma vez por
    janela, e o prefetch já é essa consulta. Uma thumbnail ausente não diz
    nada sobre se os pixels mudaram, e recalcular o embedding de uma foto
    porque a thumbnail dela não foi gerada gastaria a operação mais cara do
    sistema para consertar a mais barata.

    O próprio local, e não uma flag, embora o backfill só precise da flag.
    `GET /images/{id}/thumbnail` precisa do local e do `content_hash` ao lado
    dele, e um booleano aqui teria deixado a coluna sem leitor nenhum -- a
    rota teria de reconstruir o local a partir do id, e o gravado teria
    virado uma segunda cópia de um fato livre para discordar da primeira.

    Só `ImageRepository.save_indexed*()` e `update_thumbnail_path*()` o
    escrevem; `update_index_metadata()` o deixa em paz.
    """

    @property
    def thumbnail_generated(self) -> bool:
        """Se alguma thumbnail já foi guardada para esta linha."""
        return self.thumbnail_path is not None
