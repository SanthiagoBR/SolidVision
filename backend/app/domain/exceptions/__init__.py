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
from app.domain.exceptions.domain_error import (
    ConflictError,
    DomainError,
    GoneError,
    NotFoundError,
)
from app.domain.exceptions.file_access_errors import (
    FileGoneError,
    ThumbnailNotFoundError,
)
from app.domain.exceptions.image_errors import (
    ImageAlreadyExistsError,
    ImageNotFoundError,
    InvalidImageIdentifierError,
    InvalidImagePathError,
    UnsupportedImageExtensionError,
)
from app.domain.exceptions.job_errors import (
    DeviceBusyError,
    IllegalJobTransitionError,
    InvalidJobIdentifierError,
    InvalidJobScopeError,
    JobNotFoundError,
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
    "ConflictError",
    "DeviceBusyError",
    "DeviceNotConnectedError",
    "DeviceNotFoundError",
    "DomainError",
    "EmbeddingDimensionMismatchError",
    "EmptySearchQueryError",
    "FileGoneError",
    "GoneError",
    "IllegalJobTransitionError",
    "ImageAlreadyExistsError",
    "ImageNotFoundError",
    "InvalidCaptureDateError",
    "InvalidDateRangeError",
    "InvalidDeviceIdentifierError",
    "InvalidEmbeddingVectorError",
    "InvalidImageIdentifierError",
    "InvalidImagePathError",
    "InvalidJobIdentifierError",
    "InvalidJobScopeError",
    "InvalidSearchLimitError",
    "InvalidVolumeIdentityError",
    "JobNotFoundError",
    "NotFoundError",
    "ThumbnailNotFoundError",
    "UnsupportedImageExtensionError",
]
