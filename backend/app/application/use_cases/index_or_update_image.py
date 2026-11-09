"""Caso de uso da aplicação para a indexação incremental guiada pelo disco."""

from __future__ import annotations

import datetime

from app.application.use_cases.capture_date_plan import capture_date_to_write
from app.application.use_cases.indexing_plan import (
    IndexAction,
    IndexCandidate,
    plan_indexing,
)
from app.application.use_cases.position_plan import position_to_write
from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_record import IndexingRecord


class IndexOrUpdateImageUseCase:
    """Cria, atualiza ou pula uma única imagem conforme o seu estado no disco.

    Separado de `IndexImageUseCase` (RFC-015), que sempre cria e rejeita
    duplicatas. Este caso de uso roda a verificação incremental em três passos
    do `ARCHITECTURE.md` §16 -- ver `plan_indexing()`, que é dona da decisão
    em si:

    - nenhum metadado persistido -> arquivo novo, indexar;
    - há metadados persistidos, mas os dois campos são `None` (a imagem foi
      gravada pelo `save()` simples de `IndexImageUseCase`) -> tratada como
      alterada, não como inalterada, e por isso é indexada e preenchida;
    - os metadados persistidos batem exatamente com o disco -> pular, sem
      nem ler o arquivo;
    - os metadados diferem, mas o hash de conteúdo prova que os bytes são
      idênticos -> atualizar os metadados guardados e manter o embedding;
    - os metadados diferem e o conteúdo mudou de fato -> reindexar.

    Desde o RFC-028, uma imagem pulada cuja linha nunca foi examinada para
    data de captura recebe também a data que `image` traz, sem refazer o
    embedding -- e, desde o RFC-032, a sua posição, decidida à parte por
    `position_to_write()`.

    O RFC-024 manteve esta classe de propósito, em vez de aposentá-la em favor
    de `IndexOrUpdateImagesUseCase`. Ela é a porta de entrada de um arquivo só:
    a lógica de decisão é compartilhada com o coordenador de lotes por
    `plan_indexing()`, então não há segunda cópia de nada, mas aqui os erros se
    propagam para quem chama em vez de serem reunidos num resumo da execução.
    Esse é o contrato certo para indexar um arquivo conhecido -- um futuro
    caminho de reindexação de um arquivo, ou de observação de arquivos, quer
    ouvir que o seu arquivo falhou, não receber um relatório dizendo que zero
    de um deu certo.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        content_hasher: ContentHasherPort,
    ) -> None:
        self._repository = repository
        self._embedding_model = embedding_model
        self._content_hasher = content_hasher

    def execute(
        self,
        image: Image,
        file_size: int | None,
        file_modified_at: datetime.datetime | None,
    ) -> bool:
        """Indexa `image`, pulando-a quando o seu conteúdo não mudou.

        Devolve `True` quando a imagem foi (re)indexada, `False` quando não foi
        preciso um embedding novo -- seja porque nada mudou no arquivo, seja
        porque só os seus metadados de sistema de arquivos mudaram.
        """
        candidate = IndexCandidate(
            image=image,
            file_size=file_size,
            file_modified_at=file_modified_at,
        )
        existing = self._repository.get_index_metadata(image.id)
        plan = plan_indexing(candidate, existing, self._content_hasher)

        if plan.action is not IndexAction.EMBED:
            # Os dois ramos que pulam, não só um: ver
            # `IndexOrUpdateImagesUseCase._write_capture_dates()`.
            if plan.action is IndexAction.REFRESH_METADATA:
                self._repository.update_index_metadata(
                    image.id,
                    IndexMetadata(
                        file_size=file_size,
                        file_modified_at=file_modified_at,
                        content_hash=plan.content_hash,
                    ),
                )
            capture = capture_date_to_write(
                existing.capture_source if existing else None, image.capture_date
            )
            if capture is not None:
                self._repository.update_capture_date(image.id, capture)
            position = position_to_write(
                existing.position_source if existing else None,
                image.position_reading,
            )
            if position is not None:
                self._repository.update_position(image.id, position)
            return False

        embedding = self._embedding_model.encode_image(image)
        record = IndexingRecord(
            image=image,
            embedding=embedding,
            file_size=file_size,
            file_modified_at=file_modified_at,
            content_hash=plan.content_hash,
        )
        self._repository.save_indexed(record)
        return True
