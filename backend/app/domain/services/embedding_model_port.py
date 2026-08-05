"""Abstract contract for components that generate embeddings from domain objects."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector


class EmbeddingModelPort(ABC):
    """Generate embeddings from domain data without mutating the supplied inputs.

    Implementations must treat the provided domain objects as read-only and must not
    mutate the supplied Image instance or the text string.
    """

    @abstractmethod
    def encode_image(self, image: Image) -> EmbeddingVector:
        """Return an embedding for the supplied image."""

    @abstractmethod
    def encode_text(self, text: str) -> EmbeddingVector:
        """Return an embedding for the supplied text."""
