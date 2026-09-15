"""Capture-date domain exceptions (RFC-028)."""

from app.domain.exceptions.domain_error import DomainError


class InvalidCaptureDateError(DomainError):
    """Raised when a capture date and its source do not describe one fact.

    Most often: a zone-aware `datetime`. EXIF records the camera's local
    wall-clock time and no zone (RFC-028 section 5), so an aware value
    means some layer invented one, and the invented zone is exactly what
    moves a New Year's Eve photo into the wrong year.
    """

    def __init__(self, message: str = "Invalid capture date.") -> None:
        super().__init__(message)


class InvalidDateRangeError(DomainError):
    """Raised when a capture-date range cannot be evaluated as asked.

    A subclass of `DomainError`, so the HTTP layer answers 400 without a
    handler of its own: an inverted range, or one bound sent with a UTC
    offset, is a well-formed request the application refuses -- which is
    what 400 means there, as opposed to FastAPI's 422 for a request that
    does not parse at all.
    """

    def __init__(self, message: str = "Invalid date range.") -> None:
        super().__init__(message)
