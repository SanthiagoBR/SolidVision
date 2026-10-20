"""The two checks in front of everything the API serves (RFC-037 section 5.2).

    rule                                                       refusal
    the name in `Host`, without the port, is `127.0.0.1` or    400
    `localhost`
    a method other than GET, HEAD or OPTIONS that carries an   403
    `Origin` comes from `http://127.0.0.1` or
    `http://localhost`, on any port; `Origin: null` is refused

One ASGI middleware, outside the routing: `/health`, the interface and an
unknown path are checked like any route.

**The `Host` closes DNS rebinding.** A page on `evil.example` whose name starts
resolving to `127.0.0.1` makes its requests with `Host: evil.example:<port>`,
and reads the answers as if they were its own origin's. Those are refused here,
before anything is read.

**The `Origin` closes the form.** What a page does without reading the answer --
a `<form method="post">` straight to `http://127.0.0.1:<port>` -- carries the
right `Host`. The routes with a JSON body already refuse it, because a form
cannot send `application/json`; the two `POST`s without a body, `/reveal` and
`/cancel`, want ids a page has no way to know. The risk is small, and the
browser sends `Origin` with every cross-site `POST`, so checking it is a line
here. An absent `Origin` passes: `curl`, the measurement scripts, the
`TestClient` and the launcher's probe send none. The window sends its own, and
the proxy of `npm run dev` sends `localhost:5173`, which passes.

What stays open is a process on this machine, which reaches loopback with any
header it likes. That is the per-session token's, which RFC-036 section 14 left
out, and this RFC too.

**`testserver`, the `TestClient`'s default, is not accepted.** Accepting it would
accept a name with no domain, which the local network resolves (LLMNR, NetBIOS)
and somebody on it can answer. The suite asks `http://127.0.0.1` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost"})
"""The names this API answers to. Not `::1`: it listens on `127.0.0.1` only."""

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""The methods whose `Origin` is not checked: they change nothing."""


@dataclass(frozen=True)
class Refusal:
    status: int
    detail: str


def host_name(host: str) -> str:
    """The name of a `Host` header, without the port, in lower case.

    `[::1]:8000` comes out as `[`, which is not a loopback name here, and that is
    right: nothing listens on IPv6.
    """
    return host.strip().partition(":")[0].lower()


def origin_is_loopback(origin: str) -> bool:
    """`http://127.0.0.1` or `http://localhost`, on any port. `null` is not."""
    try:
        parts = urlsplit(origin.strip())
        name = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "http" and name in LOOPBACK_NAMES


def refusal_for(method: str, host: str | None, origin: str | None) -> Refusal | None:
    """Whether a request is refused, and how; `None` lets it through."""
    if host is None or host_name(host) not in LOOPBACK_NAMES:
        return Refusal(400, "Invalid host header")
    if (
        method.upper() not in READ_METHODS
        and origin is not None
        and not origin_is_loopback(origin)
    ):
        return Refusal(403, "Cross-origin requests that change something are refused")
    return None


class LoopbackRequestGuard:
    """The middleware: `refusal_for` on every HTTP request, before the routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # The lifespan. No route of this API is a WebSocket.
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        method = str(scope["method"])
        refusal = refusal_for(method, headers.get("host"), headers.get("origin"))
        if refusal is None:
            await self.app(scope, receive, send)
            return
        # `%r`, so that whatever the header holds stays on one line of the log.
        logger.warning(
            "Refused %s %s with %d: Host %r, Origin %r",
            method,
            scope["path"],
            refusal.status,
            headers.get("host"),
            headers.get("origin"),
        )
        response = JSONResponse({"detail": refusal.detail}, status_code=refusal.status)
        await response(scope, receive, send)
