"""Translation from domain failures to HTTP status codes (RFC-026 section 8).

One handler for the whole `DomainError` hierarchy, registered on the app
rather than caught in a route. Two properties follow from that, and both
are the point:

* every route stays free of `try/except`, so it keeps the shape RFC-026
  section 4 requires -- call the use case, map the result, return;
* every domain error a future use case raises already has a status code,
  without anyone remembering to add one.

**RFC-029 added two statuses without adding a registry.** The status comes
from where an error sits in the hierarchy, not from a table of exception
classes kept in Presentation: `NotFoundError` answers 404, `ConflictError`
answers 409, and everything else still answers 400. A new "not found"
error inherits its status; it does not have to be registered anywhere, and
nobody can forget to. RFC-030 added a third base the same way:
`GoneError` answers 410.

    situation                          error                         status
    ---------------------------------- ----------------------------- ------
    job id names nothing               JobNotFoundError                 404
    device id names nothing            DeviceNotFoundError              404
    image id names nothing             ImageNotFoundError               404
    image has no thumbnail to serve    ThumbnailNotFoundError           404
    device already has an active job   DeviceBusyError                  409
    cancelling a finished job          IllegalJobTransitionError        409
    device is not plugged in           DeviceNotConnectedError          409
    disk plugged in, file not on it    FileGoneError                    410
    scope is absolute / has .. / gone  InvalidJobScopeError             400

The last two rows are worth reading together. RFC-029 section 7.1 sent
"device disconnected" to 400; that is wrong, and the correction is
recorded in the RFC. 400 says *the request needs fixing*, and nothing
about that request does -- the identical bytes succeed once the disk is
plugged in. An invalid scope is the opposite: it would not start working
whatever happened in the world.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    ConflictError,
    DomainError,
    GoneError,
    NotFoundError,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

STATUS_BY_BASE: tuple[tuple[type[DomainError], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ConflictError, status.HTTP_409_CONFLICT),
    (GoneError, status.HTTP_410_GONE),
)
"""The status-bearing bases, and nothing else.

Deliberately short, and it must stay that way: an entry per exception
class would be the registry this design exists to avoid, and would drift
the first time somebody added an error without touching this file.
"""

DEFAULT_STATUS = status.HTTP_400_BAD_REQUEST
"""What a plain `DomainError` still means: understood, and refused.

400, not 422, and the difference is deliberate. FastAPI already returns
422 for a request that does not match the endpoint at all -- a missing
`q`, a `limit` that is not an integer -- and that means something
different: *this is not a well-formed request*. A `DomainError` means the
request was understood and the application refused it. Flattening the two
would erase that distinction and put Presentation in the business of
re-describing failures the Application layer never saw.
"""


def status_for(error: Exception) -> int:
    """Return the status of the most specific matching base, or 400.

    "Most specific" is resolved through the exception's own MRO rather
    than through the order of the table above, so the answer does not
    depend on how the tuple happens to be written. With three disjoint
    bases that is the same answer either way today; it stops being the same
    answer the moment a third base is added under one of these two, which
    is exactly when a subtle mapping bug would be hardest to see.
    """
    mro = type(error).__mro__
    matches = [
        (mro.index(base), code)
        for base, code in STATUS_BY_BASE
        if isinstance(error, base)
    ]
    if not matches:
        return DEFAULT_STATUS
    return min(matches)[1]


def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a refused-but-understood request with its status and message.

    The parameter is typed `Exception` rather than `DomainError` because
    that is the signature Starlette's handler registry declares;
    `add_exception_handler` below is what narrows it to the subclass this
    actually receives.

    The message is the domain error's own. Those messages are written to
    be read by whoever made the request -- "Device 'HD2' is not connected.
    Plug it in and try again." -- and rewriting them here would mean
    Presentation describing failures it never saw.
    """
    code = status_for(exc)
    logger.info("Domain error refused a request: %s -> %d", type(exc).__name__, code)
    return JSONResponse(status_code=code, content={"detail": str(exc)})


def register_error_handlers(app: FastAPI) -> None:
    """Attach the domain-error mapping to `app`."""
    app.add_exception_handler(DomainError, domain_error_handler)
