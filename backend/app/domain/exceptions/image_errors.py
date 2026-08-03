"""Image-specific domain exceptions."""

from app.domain.exceptions.domain_error import DomainError


class ImageNotFoundError(DomainError):
    """Raised when an image cannot be found."""

    def __init__(self, message: str = "Image not found.") -> None:
        super().__init__(message)


class ImageAlreadyExistsError(DomainError):
    """Raised when an image already exists in a context where it should not."""

    def __init__(self, message: str = "Image already exists.") -> None:
        super().__init__(message)


class InvalidImageIdentifierError(DomainError):
    """Raised when an image identifier is invalid."""

    def __init__(self, message: str = "Image identifier must be a valid UUID") -> None:
        super().__init__(message)


class InvalidImagePathError(DomainError):
    """Raised when an image path is invalid."""

    def __init__(self, message: str = "Invalid image path.") -> None:
        super().__init__(message)


class UnsupportedImageExtensionError(DomainError):
    """Raised when an image extension is not supported."""

    def __init__(self, message: str = "Unsupported image extension.") -> None:
        super().__init__(message)
