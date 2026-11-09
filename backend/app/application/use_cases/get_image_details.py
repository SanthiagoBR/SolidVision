"""Uma imagem indexada, onde ela está, e se o disco dela está aqui (RFC-030 §4.2).

`GET /api/v1/images/{id}` -- a rota que o `search_schema.py` do RFC-026 prometeu
ao "RFC-027", que virou o de dispositivos, e que o RFC-030 entrega três números
depois.
"""

from __future__ import annotations

from app.application.use_cases.resolve_image_location import (
    LocatedImage,
    ResolveImageLocationUseCase,
)
from app.domain.exceptions import ImageNotFoundError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId


class GetImageDetailsUseCase:
    """Devolve uma imagem com o seu dispositivo e, se montado, o caminho absoluto."""

    def __init__(
        self,
        repository: ImageRepository,
        location_resolver: ResolveImageLocationUseCase,
    ) -> None:
        self._repository = repository
        self._locations = location_resolver

    def execute(self, image_id: ImageId) -> LocatedImage:
        """Devolve a imagem, levantando exceção só quando o id não nomeia linha nenhuma.

        **Um dispositivo desconectado é uma resposta bem-sucedida.** A linha
        existe, e é a linha que isto descreve; `connected: false` com o rótulo
        do dispositivo é o resultado "está no HD3" que o RFC-030 §4.1 existe
        para dar, e um 404 ou um 409 aqui o transformaria num erro.

        Sem similaridade, porque não há consulta: um score não significa nada
        fora da busca que o produziu (RFC-025 §4.1).
        """
        image = self._repository.get(image_id)
        if image is None:
            raise ImageNotFoundError(f"No indexed image with id {image_id.value}.")
        return self._locations.execute_one(image)
