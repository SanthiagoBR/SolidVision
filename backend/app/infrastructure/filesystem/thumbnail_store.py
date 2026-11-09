"""Thumbnails em disco local, num diretório que pertence à aplicação (RFC-030 §7.1).

O diretório é `settings.thumbnail_directory`, e a única regra que importa sobre
ele é onde ele *não* fica: nunca dentro da coleção de um usuário. O RFC-028 §10
diz que o sistema nunca escreve na coleção, e o RFC-030 §7.1 acrescenta a razão
específica das thumbnails -- guardadas ao lado das fotos, elas iriam para a
gaveta junto com o disco, que é exatamente quando são o único jeito de ver o que
uma busca encontrou.

O scan de indexação pula este diretório mesmo quando ele está dentro de uma
pasta varrida (`FilesystemImageProvider(excluded_directories=...)`). Sem isso,
indexar um disco de sistema inteiro descobriria as thumbnails como fotos,
renderizaria thumbnails delas e descobriria essas na execução seguinte.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.value_objects.image_id import ImageId
from app.infrastructure.filesystem.thumbnail_generator import THUMBNAIL_SUFFIX


class FilesystemThumbnailStore(ThumbnailStorePort):
    """Um JPEG por id de imagem, distribuído pelos dois primeiros dígitos hex do id."""

    def __init__(self, directory: Path) -> None:
        """Guarda as thumbnails sob `directory`, que é criado na primeira escrita.

        Resolvido uma vez aqui, para que uma configuração relativa signifique o
        mesmo diretório em toda chamada, e para que `locate()` tenha uma raiz
        fixa contra a qual checar uma localização.
        """
        self._directory = directory.resolve()

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, image_id: ImageId, data: bytes) -> str:
        """Escreve `data` para `image_id` atomicamente e devolve a localização.

        **A localização é relativa ao diretório do store**, então mover o cache
        significa mover uma pasta, e não reescrever todas as linhas -- o
        argumento que o RFC-027 fez para `relative_path`.

        **Distribuído pelos dois primeiros dígitos hex do id.** Cem mil arquivos
        num diretório funcionam, mas tornam o diretório inutilizável no Explorer
        e lento de copiar para backup; 256 subdiretórios de algumas centenas de
        arquivos cada não custam nada para endereçar, porque o subdiretório é
        derivado do id, e não consultado.

        **Escrito com um nome temporário e depois renomeado sobre o destino.**
        O mesmo id é sobrescrito sempre que os seus bytes mudam (RFC-030 §7.2),
        e uma requisição servida durante uma escrita simples transmitiria meio
        JPEG. `os.replace` é atômico no mesmo volume. O nome temporário não
        termina em `.jpg`, então mesmo um scan que percorresse este diretório
        não o tomaria por foto. Se a substituição falhar -- no Windows, porque
        uma resposta está transmitindo o arquivo antigo naquele instante -- o
        erro se propaga e quem chama registra uma falha de thumbnail; o arquivo
        temporário é removido.
        """
        hex_id = image_id.value.hex
        location = f"{hex_id[:2]}/{image_id.value}{THUMBNAIL_SUFFIX}"
        target = self._directory / location
        target.parent.mkdir(parents=True, exist_ok=True)

        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return location

    def locate(self, location: str) -> Path | None:
        """Devolve o arquivo guardado para `location`, ou `None` se não estiver lá.

        A localização veio do próprio banco desta aplicação, não de um cliente,
        e ainda assim é checada contra o diretório: uma linha editada à mão para
        `../../algum-lugar` não pode transformar o endpoint de thumbnails num
        jeito de ler um arquivo qualquer. A checagem é sobre o caminho resolvido,
        então um link dentro do cache apontando para fora dele também é
        recusado.
        """
        candidate = (self._directory / location).resolve()
        if not candidate.is_relative_to(self._directory):
            return None
        if not candidate.is_file():
            return None
        return candidate

    def delete(self, location: str) -> None:
        """Remove o arquivo por trás de `location`; nada lá, nada feito (RFC-046).

        Achado por `locate()`, então uma localização que resolve para fora do
        diretório do store não remove nada, assim como não serve nada. O
        subdiretório fica: ele é um de 256, e a próxima thumbnail de um id que
        comece igual vai precisar dele de novo.
        """
        found = self.locate(location)
        if found is not None:
            found.unlink(missing_ok=True)
