"""Portador que junta tudo o que é preciso para persistir uma imagem indexada."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector


@dataclass(frozen=True)
class IndexingRecord:
    """Portador só de escrita que junta uma imagem ao embedding e aos metadados.

    Existe porque `ImageRepository.save()` não tem como levar um embedding ou
    metadados de sistema de arquivos junto com uma `Image` do Domain. Ele faz
    parte do contrato da própria porta `ImageRepository` -- como
    `EmbeddingVector` ou `ImageId` -- então mora no Domain, e não na
    Application, o que mantém a porta autocontida na própria camada. A
    `Image` em si continua completamente inalterada. Não é usado como tipo de
    leitura; para isso, ver `IndexMetadata`.
    """

    image: Image
    embedding: EmbeddingVector
    file_size: int | None
    file_modified_at: datetime.datetime | None
    content_hash: str | None = None
    """Impressão digital dos bytes que produziram `embedding`.

    Opcional pela mesma razão de `IndexMetadata.content_hash`: um escritor
    que nunca calculou uma grava NULL, e NULL é relido como "desconhecido", o
    que custa recalcular o embedding na próxima mudança de tamanho ou mtime,
    em vez de arriscar pular um.
    """

    thumbnail_path: str | None = None
    """Onde a thumbnail gerada destes bytes foi guardada (RFC-030).

    Opcional como `content_hash`, e `None` é escrito como `None`: um registro
    chega ao repositório porque os bytes dele são novos ou mudaram, então uma
    thumbnail guardada para uma versão *anterior* do arquivo não o mostra
    mais. Manter esse local velho serviria a foto antiga sob o `ETag` novo.

    `None` quando a geração falhou, o que nunca é uma falha do registro -- o
    embedding foi pago e a imagem é buscável sem thumbnail (RFC-030 §7.2) --
    ou quando o pipeline foi montado sem escritor de thumbnails nenhum.
    """
