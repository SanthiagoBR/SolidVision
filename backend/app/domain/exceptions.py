"""Domain-specific exceptions for the SolidVision backend."""


class InvalidImagePathError(ValueError):
    """Raised when an image path is invalid."""


class InvalidImageIdentifierError(ValueError):
    """Raised when an image identifier is invalid."""
