"""Translation from domain failures to HTTP status codes (RFC-026 section 8).

One handler for the whole `DomainError` hierarchy, registered on the app
rather than caught in a route. Two properties follow from that, and both
are the point:

* every route stays free of `try/except`, so it keeps the shape RFC-026
  section 4 requires -- call the use case, map the result, return;
* every domain error a future use case raises already has a status code,
  without anyone remembering to add one.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import DomainError
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a refused-but-understood request with 400 and its message.

    400, not 422, and the difference is deliberate. FastAPI already
    returns 422 for a request that does not match the endpoint at all --
    a missing `q`, a `limit` that is not an integer -- and that means
    something different: *this is not a well-formed request*. A
    `DomainError` means the request was understood and the application
    refused it. Flattening the two would erase that distinction and put
    Presentation in the business of re-describing failures the
    Application layer never saw.

    The parameter is typed `Exception` rather than `DomainError` because
    that is the signature Starlette's handler registry declares;
    `add_exception_handler` below is what narrows it to the subclass this
    actually receives.
    """
    logger.info("Domain error refused a request: %s", type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach the domain-error mapping to `app`."""
    app.add_exception_handler(DomainError, domain_error_handler)
