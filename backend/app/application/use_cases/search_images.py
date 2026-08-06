"""Application use case for searching images by text query."""

from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort


class SearchImagesUseCase:
    """Coordinate repository listing and embedding generation for searches."""

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
    ) -> None:
        self._repository = repository
        self._embedding_model = embedding_model

    def execute(self, query: str) -> list[Image]:
        """Return images from the repository for the supplied query."""
        self._embedding_model.encode_text(query)
        return self._repository.list()
