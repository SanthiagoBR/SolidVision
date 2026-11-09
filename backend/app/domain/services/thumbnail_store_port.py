"""Onde ficam as thumbnails geradas, sem nomear um diretório (RFC-030).

A camada Application decide *que* uma thumbnail é escrita e grava onde; ela
não pode abrir arquivos para isso, assim como não calcula o hash deles. Esta
porta é a outra metade de `ThumbnailGeneratorPort`: aquela transforma uma
foto em bytes, esta guarda os bytes.

**Nada atrás desta porta pode escrever na coleção de um usuário.** O RFC-028
§10 declarou que o sistema nunca escreve na coleção, e o RFC-030 §7.1 o
repete para as thumbnails com uma segunda razão: uma thumbnail guardada ao
lado da foto sumiria na gaveta junto com o disco, que é exatamente quando o
usuário mais precisa ver o que achou.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.domain.value_objects.image_id import ImageId


class ThumbnailStorePort(ABC):
    """Guarda uma thumbnail por id de imagem, num armazenamento do aplicativo."""

    @abstractmethod
    def save(self, image_id: ImageId, data: bytes) -> str:
        """Guarda `data` como a thumbnail de `image_id` e devolve o local dela.

        O local é o que vai para `images.thumbnail_path`, e é opaco para todos
        menos o store: um chamador o devolve a `locate()` e nunca monta um
        caminho a partir dele.

        Guardar para um id que já tem thumbnail a substitui no lugar. É a
        regeneração que o RFC-030 §7.2 descreve -- o mesmo id, bytes novos --
        e a razão de os clientes precisarem revalidar, em vez de confiar
        para sempre numa cópia em cache.
        """

    @abstractmethod
    def locate(self, location: str) -> Path | None:
        """Devolve o arquivo por trás de um local gravado, ou `None` se ele sumiu.

        `None` é uma resposta honesta, e não um erro: o cache é do
        aplicativo, e uma thumbnail que foi limpa é servida como "ainda não
        gerada" (RFC-030 §7.2), em vez de derrubar a requisição.
        """

    @abstractmethod
    def delete(self, location: str) -> None:
        """Remove a thumbnail por trás de um local gravado, se ela existe (RFC-046).

        O segundo passo da purga, depois que as linhas se foram (§5.7). Um
        local sem nada por trás não é erro -- o cache é do aplicativo, e o
        arquivo pode já ter sido limpo -- e um local que não resolve para
        dentro do store não apaga nada: a mesma verificação que `locate()`
        faz, porque este remove.
        """
