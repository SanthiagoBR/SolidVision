"""Indexing-job domain exceptions (RFC-029)."""

from app.domain.exceptions.domain_error import (
    ConflictError,
    DomainError,
    NotFoundError,
)


class JobNotFoundError(NotFoundError):
    """Raised when no indexing job carries the requested id.

    Distinct from "the job exists and has not started", which is a
    perfectly ordinary `pending` job a client is polling. Collapsing the
    two would make a typo in an id indistinguishable from a queue that is
    merely slow (RFC-029 section 7.2).
    """

    def __init__(self, message: str = "Indexing job not found.") -> None:
        super().__init__(message)


class InvalidJobIdentifierError(DomainError):
    """Raised when a job identifier is not a UUID.

    A 400 rather than a 404, and the difference is real: `GET
    /api/v1/jobs/banana` never named a job, whereas a well-formed id
    that matches no row did and is a `JobNotFoundError`.
    """

    def __init__(self, message: str = "Job identifier must be a valid UUID") -> None:
        super().__init__(message)


class DeviceBusyError(ConflictError):
    """Raised when a device already has a pending or running indexing job.

    The Python-side face of the partial unique index of RFC-029 section 9,
    never a replacement for it. The repository translates the database's
    refusal into this; nothing asks the database whether a job exists
    before inserting one, because that would be the check-then-act the
    index exists to avoid.
    """

    def __init__(self, message: str = "Device already has an active job.") -> None:
        super().__init__(message)


class IllegalJobTransitionError(ConflictError):
    """Raised when a job is asked to move somewhere its current state forbids.

    Cancelling a `completed` job raises this rather than returning
    quietly, because RFC-029 section 8 is explicit that a caller asking
    for it is operating on a wrong assumption and deserves to find out. A
    silent no-op would let a UI show "cancelling..." over a job that
    finished an hour ago.
    """

    def __init__(self, message: str = "Illegal indexing job transition.") -> None:
        super().__init__(message)


class InvalidJobScopeError(DomainError):
    """Raised when a scope is not a usable folder within a device.

    A plain `DomainError`, so 400: an absolute scope, a scope containing
    `..`, a UNC path, or one naming a folder that is not on the disk are
    all *malformed requests*, and none of them would start working if the
    state of the world changed. That is what separates them from
    `DeviceBusyError` and `DeviceNotConnectedError`, which are the same
    request at a bad moment (RFC-029 section 7.1, corrected).
    """

    def __init__(self, message: str = "Invalid indexing job scope.") -> None:
        super().__init__(message)
