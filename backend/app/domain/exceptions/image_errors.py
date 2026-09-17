"""Image-specific domain exceptions."""

from app.domain.exceptions.domain_error import DomainError, NotFoundError


class ImageNotFoundError(NotFoundError):
    """Raised when an image cannot be found.

    Re-based from `DomainError` onto `NotFoundError` by RFC-030, which
    gave the first route able to raise it: `GET /api/v1/images/{id}` with
    an id that names no row is a 404 (RFC-030 section 4.2), and a plain
    `DomainError` answers 400. The same move RFC-029 made for
    `DeviceNotFoundError`, and safe for the same reason -- until RFC-030
    nothing in production raised this at all, so no client has ever been
    told a status for it.
    """

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
