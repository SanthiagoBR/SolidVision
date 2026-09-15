"""Public exports for domain exceptions."""

from app.domain.exceptions.capture_date_errors import (
    InvalidCaptureDateError,
    InvalidDateRangeError,
)
from app.domain.exceptions.device_errors import (
    DeviceNotConnectedError,
    DeviceNotFoundError,
    InvalidDeviceIdentifierError,
    InvalidVolumeIdentityError,
)
from app.domain.exceptions.domain_error import DomainError
from app.domain.exceptions.image_errors import (
    ImageAlreadyExistsError,
    ImageNotFoundError,
    InvalidImageIdentifierError,
    InvalidImagePathError,
    UnsupportedImageExtensionError,
)
from app.domain.exceptions.search_errors import (
    EmbeddingDimensionMismatchError,
    EmptySearchQueryError,
    InvalidSearchLimitError,
)


class InvalidEmbeddingVectorError(DomainError):
    """Raised when an embedding vector is invalid."""

    def __init__(self, message: str = "Invalid embedding vector.") -> None:
        super().__init__(message)


__all__ = [
    "DeviceNotConnectedError",
    "DeviceNotFoundError",
    "DomainError",
    "EmbeddingDimensionMismatchError",
    "EmptySearchQueryError",
    "ImageAlreadyExistsError",
    "ImageNotFoundError",
    "InvalidCaptureDateError",
    "InvalidDateRangeError",
    "InvalidDeviceIdentifierError",
    "InvalidEmbeddingVectorError",
    "InvalidImageIdentifierError",
    "InvalidImagePathError",
    "InvalidSearchLimitError",
    "InvalidVolumeIdentityError",
    "UnsupportedImageExtensionError",
]
