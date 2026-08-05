"""Public exports for domain exceptions."""

from app.domain.exceptions.domain_error import DomainError
from app.domain.exceptions.image_errors import (
    ImageAlreadyExistsError,
    ImageNotFoundError,
    InvalidImageIdentifierError,
    InvalidImagePathError,
    UnsupportedImageExtensionError,
)


class InvalidEmbeddingVectorError(DomainError):
    """Raised when an embedding vector is invalid."""

    def __init__(self, message: str = "Invalid embedding vector.") -> None:
        super().__init__(message)


__all__ = [
    "DomainError",
    "ImageAlreadyExistsError",
    "ImageNotFoundError",
    "InvalidEmbeddingVectorError",
    "InvalidImageIdentifierError",
    "InvalidImagePathError",
    "UnsupportedImageExtensionError",
]
