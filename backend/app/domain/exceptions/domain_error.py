"""Root exception for the domain layer, and the two bases that carry a status.

`DomainError` alone was enough while every refusal meant the same thing:
*the request was understood and the application declined it*, which
`error_handlers.py` answered with 400. RFC-029 introduces refusals that
400 actively misdescribes -- a job id that names nothing, a disk that is
already busy -- so two intermediate bases were added rather than a table
of exception classes in Presentation.

The bases are here, beside `DomainError`, rather than in a file of their
own: they are the hierarchy's root, and a caller that imports `DomainError`
is exactly the caller that needs to know these two exist.

**They name a *kind of refusal*, not an HTTP code.** `NotFoundError` means
the thing referred to does not exist; `ConflictError` means it exists and
the current state does not permit what was asked. That Presentation maps
them to 404 and 409 is Presentation's business -- the Domain still never
returns HTTP codes (`ARCHITECTURE.md` section 18).
"""


class DomainError(Exception):
    """Base class for all domain-layer exceptions."""


class NotFoundError(DomainError):
    """Raised when an operation names something that does not exist.

    Deliberately *not* used for an empty result. "No images matched this
    query" is a successful answer; "there is no job with this id" is a
    caller operating on a wrong assumption, and the distinction is the
    one RFC-029 section 7 needs so that polling a job can tell "not
    finished yet" from "never existed".
    """


class ConflictError(DomainError):
    """Raised when the request is well formed but the current state refuses it.

    The test is whether the same request would succeed later, or with the
    world in a different state: a job creation for a disk that already has
    an active job would be accepted once that job ends, and one for an
    unplugged disk would be accepted with the disk plugged in. Nothing
    about the request needs fixing, which is precisely what separates this
    from the 400 case.
    """
