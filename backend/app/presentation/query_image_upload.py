"""The picture of `POST /images/search/by-image`, from the body to a file (RFC-038 4.2).

    check                                       refusal
    `Content-Type` is image/jpeg or image/png   415
    `Content-Length`, when sent, is <= 50 MB    413, before reading
    the body, counted as it arrives, <= 50 MB   413, cut at the ceiling
    the first bytes are the declared type's     415
    Pillow decodes it, under its pixel limit    400 (UnreadableQueryImageError)

**The body is the bytes**, not `multipart/form-data`: there is no other field
to send, and `python-multipart`, which FastAPI needs for forms, is not
installed. The type being required also closes the form of RFC-037 section
2.3 for this route: a `<form>` sends only urlencoded, multipart or plain text,
and a `fetch` from another origin with `image/jpeg` triggers a preflight this
API does not answer.

**Two dependencies, and the split is the event loop's.** The body is read in
an `async` one, into memory, because reading a request is I/O the loop does
well; writing it to disk there would hold every other request -- `/health`
included -- for the length of the write. The file is written, decoded and
deleted in a plain generator, which FastAPI runs in its threadpool, and whose
`finally` runs however the request ends: success, refusal or error.

**No path comes in** (RFC-030 section 5.1): the body is bytes, and the
temporary file is the server's, with a random name, in
`cache\\query-images` under the data root. The folder is emptied when the
API starts, for what a process that died left behind.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status

from app.infrastructure.config.layout import layout
from app.infrastructure.filesystem.query_image import inspect_query_image
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

MAX_QUERY_IMAGE_BYTES = 50 * 1024 * 1024
"""50 MB. A choice: a 20 MP drone JPEG is 8 to 20 MB, a 4K screenshot in PNG
about 20. A constant, and the 413 says it."""

SIGNATURES = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}
"""The media types accepted, and the bytes each file starts with."""

EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}

REQUEST_BODY = {
    "required": True,
    "description": (
        "The picture's bytes, as the body. Up to 50 MB; the type must match "
        "the file."
    ),
    "content": {
        media_type: {"schema": {"type": "string", "format": "binary"}}
        for media_type in SIGNATURES
    },
}
"""The `requestBody` of the route in the OpenAPI document, for typed clients.

The route reads the body itself, so FastAPI would publish none."""


@dataclass(frozen=True)
class QueryImageBody:
    """The bytes of the request, and the type it declared."""

    media_type: str
    data: bytes


def media_type_of(content_type: str | None) -> str | None:
    """`image/jpeg; charset=x` is `image/jpeg`; nothing is `None`."""
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


async def read_query_image_body(request: Request) -> QueryImageBody:
    """The body, refused by type and by size before anything is decoded."""
    media_type = media_type_of(request.headers.get("content-type"))
    if media_type not in SIGNATURES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Send the picture as the body, with Content-Type image/jpeg "
            "or image/png.",
        )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit():
        if int(declared) > MAX_QUERY_IMAGE_BYTES:
            raise _too_large()
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > MAX_QUERY_IMAGE_BYTES:
            raise _too_large()
    data = bytes(received)
    if not data.startswith(SIGNATURES[media_type]):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"The body does not start like a {media_type} file.",
        )
    return QueryImageBody(media_type=media_type, data=data)


def get_query_image_directory() -> Path:
    """`cache\\query-images` under the data root: the layout's, no setting.

    The seam the tests override, and the one the API's start empties -- the
    lifespan resolves it through `app.dependency_overrides` -- so that the
    folder written and the folder emptied are always the same one.
    """
    return layout.query_image_directory


def query_image_file(
    body: QueryImageBody = Depends(read_query_image_body),
    directory: Path = Depends(get_query_image_directory),
) -> Iterator[Path]:
    """The picture as a decoded file, deleted when the request is over."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{secrets.token_hex(16)}{EXTENSIONS[body.media_type]}"
    try:
        path.write_bytes(body.data)
        inspect_query_image(path, body.media_type)
        yield path
    finally:
        path.unlink(missing_ok=True)


def empty_query_image_directory(directory: Path) -> int:
    """Delete the files a process that died left in `directory`; how many.

    A missing folder is an empty one. A file that cannot be deleted -- open in
    another process -- is left, logged, and does not stop the API.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for leftover in directory.iterdir():
        if not leftover.is_file():
            continue
        try:
            leftover.unlink()
            removed += 1
        except OSError as error:
            logger.warning("A query image could not be deleted: %s", error)
    return removed


def _too_large() -> HTTPException:
    megabytes = MAX_QUERY_IMAGE_BYTES // (1024 * 1024)
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"The picture is larger than {megabytes} MB.",
    )
