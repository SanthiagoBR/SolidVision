"""Application use case for indexing a single image."""

from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.exceptions import ImageAlreadyExistsError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort


class IndexImageUseCase:
    """Coordinate repository persistence and embedding generation for an image."""

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
    ) -> None:
        self._repository = repository
        self._embedding_model = embedding_model

    def execute(self, image: Image) -> None:
        """Persist an image after verifying it does not already exist."""
        if self._repository.exists(image.id):
            raise ImageAlreadyExistsError()

        self._embedding_model.encode_image(image)
        self._repository.save(image)
