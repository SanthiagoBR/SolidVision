"""Device-specific domain exceptions (RFC-027)."""

from app.domain.exceptions.domain_error import DomainError


class InvalidDeviceIdentifierError(DomainError):
    """Raised when a device identifier is invalid."""

    def __init__(self, message: str = "Device identifier must be a valid UUID") -> None:
        super().__init__(message)


class InvalidVolumeIdentityError(DomainError):
    """Raised when a volume identity is empty or of an unknown platform kind.

    The identity is the only thing standing between a remounted disk and a
    second full re-index (RFC-027 section 2.1), so an empty or unrecognised
    one has to fail loudly at construction. The alternative -- accepting
    `""` and letting it reach the `UNIQUE` constraint -- would collapse
    every unidentifiable volume into a single device row.
    """

    def __init__(self, message: str = "Invalid volume identity.") -> None:
        super().__init__(message)


class DeviceNotFoundError(DomainError):
    """Raised when a device cannot be found."""

    def __init__(self, message: str = "Device not found.") -> None:
        super().__init__(message)


class DeviceNotConnectedError(DomainError):
    """Raised when an operation needs bytes from a volume that is not mounted.

    The honest failure for the case RFC-027 section 2.3 describes: the
    system knows exactly which image is wanted and exactly which disk it
    is on, and that disk is in a drawer. Reading it is impossible and no
    amount of retrying changes that -- what the user needs is the name of
    the disk to plug in, which the raiser is expected to put in the
    message.

    Distinct from a plain `FileNotFoundError` on purpose. "The file is
    gone" and "the disk is unplugged" call for opposite reactions: the
    first justifies dropping a row, the second must never be allowed to.
    """

    def __init__(self, message: str = "Device is not connected.") -> None:
        super().__init__(message)
