"""Deterministic fake embedding model for development and tests."""

from __future__ import annotations

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.config.settings import settings


class FakeEmbeddingModel(EmbeddingModelPort):
    """Deterministic embedding adapter that does not use any AI libraries."""

    def encode_image(self, image: Image) -> EmbeddingVector:
        seed = f"image::{image.id}::{image.path}".encode()
        return self._build_embedding(seed)

    def encode_text(self, text: str) -> EmbeddingVector:
        seed = f"text::{text}".encode()
        return self._build_embedding(seed)

    def _build_embedding(self, seed: bytes) -> EmbeddingVector:
        values: list[float] = []
        for index in range(settings.embedding_dimension):
            byte_value = seed[index % len(seed)]
            position_value = (index + 1) * 0.125
            value = ((byte_value + 1) / 256.0) + position_value
            values.append(value)

        return EmbeddingVector(values)
