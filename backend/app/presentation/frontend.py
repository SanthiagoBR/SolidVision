"""The interface, served by the API on its own origin (RFC-037 section 5.1).

    GET /assets/<file>   the files of the build, with their hashes in the name:
                         `StaticFiles`, with its `ETag`
    GET /, GET /<screen> `index.html`, with `Cache-Control: no-cache`

Same origin, so no CORS and no preflight: the window loads the URL of `ready`,
and the page talks to the origin it came from.

**The fallback is in the handling of the 404, and not in a route** (section
2.2). A `StaticFiles` mounted at `/`, or a catch-all route, would answer every
unknown path, and Starlette answers a path it knows with a method it does not
with 405: `POST /api/v1/anything` would go from 404 to 405, and the reveal
switched off -- which answers 404 so as to look like a path that does not exist
(RFC-030 section 6) -- would be the one `POST` to an "unknown" path still saying
404, pointing at the route it hides. Here the routing of the API does not
change: an unknown path is still a 404, and only then, for a `GET` or a `HEAD`
outside `/api/` and `/assets/`, the 404 becomes `index.html`. `/` itself is no
route, and goes the same way.

A missing `/assets/<file>` stays a 404. An `index.html` of an old build, pointing
at files of another one, fails visibly, instead of receiving HTML for
JavaScript.

Without an `index.html` in the directory nothing is mounted, and one line says
where the interface was looked for: `npm run dev` serves it from Vite, and the
suite needs none of it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

INDEX = "index.html"
ASSETS = "assets"
"""Where Vite puts the files with a hash in their name (`build.assetsDir`)."""

NOT_THE_INTERFACE = ("/api", f"/{ASSETS}")
"""Prefixes whose 404 stays the API's 404."""

INTERFACE_METHODS = frozenset({"GET", "HEAD"})


def is_interface_path(path: str) -> bool:
    """A path a screen of the interface may have: anything outside the two."""
    return not any(
        path == prefix or path.startswith(prefix + "/") for prefix in NOT_THE_INTERFACE
    )


def serve_frontend(app: FastAPI, directory: Path) -> bool:
    """Serve the build in `directory` from `app`, when there is one.

    `True` when it is served. The mount and the handler are added to `app`
    itself, so a test gives this an app and a directory of its own.
    """
    index = directory / INDEX
    if not index.is_file():
        logger.info("No interface in %s; the API serves only the API", directory)
        return False

    # A build always has the directory. Without it nothing is mounted, and the
    # files the page names are the API's 404s -- a `StaticFiles` over a
    # directory that is not there would answer each with a 500.
    assets = directory / ASSETS
    if assets.is_dir():
        app.mount(f"/{ASSETS}", StaticFiles(directory=assets), name=ASSETS)

    async def not_found(request: Request, exc: Exception) -> Response:
        if request.method in INTERFACE_METHODS and is_interface_path(request.url.path):
            # Read at every request: a rebuild is served without a restart.
            return FileResponse(
                index, media_type="text/html", headers={"Cache-Control": "no-cache"}
            )
        # Starlette hands a status handler only its `HTTPException`s.
        assert isinstance(exc, HTTPException)
        return await http_exception_handler(request, exc)

    app.add_exception_handler(404, not_found)
    logger.info("Serving the interface from %s", directory)
    return True
