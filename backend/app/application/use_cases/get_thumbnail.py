"""Servir uma thumbnail, e dizer a um cliente que a cópia dele é atual (RFC-030 §7.2).

A versão de uma thumbnail é o `content_hash` da foto a partir da qual ela foi
renderizada. O RFC-024 já guarda esse hash para a decisão incremental, então ele
não custa nada novo, e muda exatamente quando a thumbnail muda: uma foto
sobrescrita no lugar mantém o id -- o id é derivado do *caminho* (RFC-027) -- e
ganha um hash novo, um embedding novo e uma thumbnail nova no mesmo
reprocessamento.

`content_hash` fica fora da entidade `Image`. É um sinal de mudança, como
`file_size`, e é lido aqui por `IndexMetadata` exatamente como a decisão de
pular o lê.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from app.domain.exceptions import ImageNotFoundError, ThumbnailNotFoundError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.value_objects.image_id import ImageId


@dataclass(frozen=True)
class ThumbnailFile:
    """A thumbnail a enviar, e a versão com que rotulá-la."""

    path: Path
    version: str | None
    """O hash de conteúdo da foto, ou `None` para uma linha indexada antes do RFC-024.

    `None` significa que a thumbnail não tem versão contra a qual validar, e
    quem chama não deve inventar uma -- um validador que nunca casa é um cache
    que, em silêncio, nunca funciona.
    """


@dataclass(frozen=True)
class ThumbnailUnchanged:
    """Quem chama já tem esta versão; não é preciso enviar nada."""

    version: str


class GetThumbnailUseCase:
    """Acha a thumbnail de uma imagem, ou confirma que a cópia de quem chama vale."""

    def __init__(
        self, repository: ImageRepository, thumbnail_store: ThumbnailStorePort
    ) -> None:
        self._repository = repository
        self._store = thumbnail_store

    def execute(
        self, image_id: ImageId, held_versions: Collection[str] = ()
    ) -> ThumbnailFile | ThumbnailUnchanged:
        """Devolve o arquivo a enviar, ou diz que a versão de quem chama é atual.

        `held_versions` são as versões que quem chama diz já ter -- as tags de
        um `If-None-Match` HTTP, já sem as aspas. Compará-las não é coisa
        específica de HTTP, e é por isso que isso fica aqui e não na rota: "só
        me dê a imagem se a minha estiver desatualizada" é uma pergunta que
        qualquer cliente poderia fazer.

        **O store não é consultado quando a resposta é "inalterada".** A
        decisão precisa de uma leitura de metadados e nada mais, então
        revalidar uma página de cinquenta thumbnails custa cinquenta buscas
        escalares e nenhuma abertura de arquivo. Isso também significa que um
        cliente com a versão atual ouve isso mesmo se o arquivo do cache tiver
        sido apagado desde então -- o que é certo: a cópia dele continua sendo
        a imagem correta.

        Levanta `ImageNotFoundError` para um id sem linha, e
        `ThumbnailNotFoundError` -- também um 404 -- quando a linha não tem
        thumbnail ou o cache não tem mais o arquivo. A interface mostra um
        placeholder nos dois casos (RFC-030 §7.2).

        O dispositivo em que a foto está nunca é consultado. A thumbnail está
        no cache do próprio aplicativo, e é para isso que ela fica ali: ela
        pode ser servida enquanto o disco está na gaveta (RFC-030 §4.1).
        """
        metadata = self._repository.get_index_metadata(image_id)
        if metadata is None:
            raise ImageNotFoundError(f"No indexed image with id {image_id.value}.")
        if metadata.thumbnail_path is None:
            raise ThumbnailNotFoundError(
                f"Image {image_id.value} has no thumbnail yet."
            )

        version = metadata.content_hash
        if version is not None and version in held_versions:
            return ThumbnailUnchanged(version=version)

        path = self._store.locate(metadata.thumbnail_path)
        if path is None:
            raise ThumbnailNotFoundError(
                f"The thumbnail of image {image_id.value} is no longer in the cache."
            )
        return ThumbnailFile(path=path, version=version)
