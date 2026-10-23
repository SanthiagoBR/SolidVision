"""Address-search domain exceptions (RFC-043)."""

from app.domain.exceptions.domain_error import DomainError


class EmptyAddressError(DomainError):
    def __init__(self, message: str = "The address cannot be empty.") -> None:
        super().__init__(message)
