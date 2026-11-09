"""Gerar uma thumbnail e guardá-la, composto uma vez só (RFC-030 §7).

Duas portas e um tamanho, e dois chamadores que precisam dos três juntos: o
pipeline de indexação, que gera uma thumbnail para cada imagem que passa pelo
embedding, e o backfill de thumbnails, que gera uma para cada imagem indexada
antes do RFC-030. Compô-los aqui uma vez impede os dois chamadores de
discordarem sobre o tamanho de uma thumbnail, ou sobre para onde ela vai.
"""

from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.services.thumbnail_generator_port import ThumbnailGeneratorPort
from app.domain.services.thumbnail_store_port import ThumbnailStorePort


class ThumbnailWriter:
    """Gera `image` em `max_edge` e guarda o resultado sob o id dela."""

    def __init__(
        self,
        generator: ThumbnailGeneratorPort,
        store: ThumbnailStorePort,
        max_edge: int,
    ) -> None:
        """Compõe o writer; `max_edge` é injetado, nunca lido da configuração.

        A camada Application não pode importar `Settings`
        (`test_application_architecture.py`), então a composition root passa
        `settings.thumbnail_max_edge` -- o arranjo que o
        `IndexOrUpdateImagesUseCase` usa para o `batch_size`.
        """
        if max_edge < 1:
            raise ValueError(f"max_edge must be at least 1, got {max_edge}")
        self._generator = generator
        self._store = store
        self._max_edge = max_edge

    def write(self, image: Image, content: bytes | None = None) -> str:
        """Gera e guarda uma thumbnail, devolvendo o local guardado.

        `content` são os bytes do arquivo quando o chamador já os tem (RFC-045
        §5.3), entregues ao gerador para que ele os decodifique em vez de ler
        o arquivo de novo; o backfill não os tem e não passa nada.

        Erros de qualquer das duas portas se propagam. Se uma thumbnail que
        falhou importa é decisão do chamador, e os dois decidiram que ela não
        interrompe nada: o pipeline persiste o embedding mesmo assim, e o
        backfill passa para o próximo arquivo.
        """
        return self._store.save(
            image.id, self._generator.generate(image, self._max_edge, content)
        )
