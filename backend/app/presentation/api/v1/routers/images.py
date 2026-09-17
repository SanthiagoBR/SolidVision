"""HTTP entry points for image search and file access (RFC-026, RFC-030).

The routes own nothing. It parses a query string, hands two values to
`SearchImagesUseCase`, and shapes what comes back into JSON -- and it does
not know that similarity is a cosine, that a vector exists, that the query
may be translated before it is encoded, or that PostgreSQL is involved at
all. Every one of those lives behind the use case, which is the whole
reason the previous three RFCs kept the layers apart.

What that rules out is worth stating, because each is a plausible thing to
reach for and each would pass this module's tests: no `SessionLocal`, no
`ClipEmbeddingModel`, no `torch`, no re-sorting or filtering of the
ranking, no constructing the use case here instead of receiving it, and no
copy of the application's limit policy. `tests/test_ai_layer_boundaries.py`
walks this package's imports to keep the first four honest.

RFC-030 added three routes here, and **every one of them takes an image id
and nothing else**. No route accepts a file path in a body, a query string
or a header -- that is the security property of `/reveal` (RFC-030 section
5.1), and it holds for the other two because there is nothing a path could
be used for that the id does not already do.
"""

from __future__ import annotations

import datetime
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import FileResponse

from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_thumbnail import (
    GetThumbnailUseCase,
    ThumbnailUnchanged,
)
from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.search_filters import SearchFilters
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import (
    get_image_details_use_case,
    get_resolve_image_location_use_case,
    get_reveal_image_use_case,
    get_search_images_use_case,
    get_thumbnail_use_case,
)
from app.presentation.local_file_actions import (
    require_local_file_actions_enabled,
    require_loopback_client,
)
from app.presentation.schemas.image_schema import ImageSchema
from app.presentation.schemas.search_schema import SearchResponseSchema

router = APIRouter(prefix="/images", tags=["images"])
logger = get_logger(__name__)


@router.get(
    "/search",
    status_code=status.HTTP_200_OK,
    response_model=SearchResponseSchema,
    summary="Search indexed images by natural-language query",
)
def search_images(
    q: str = Query(description="Natural-language query, in any supported language"),
    limit: int | None = Query(
        default=None,
        description="Maximum results to return; omitted means the configured default",
    ),
    device_id: list[UUID] | None = Query(
        default=None,
        description=(
            "Restrict results to these devices; repeat the parameter for "
            "several, omit it to search every indexed device"
        ),
    ),
    captured_from: datetime.datetime | None = Query(
        default=None,
        description=(
            "Only photos taken at or after this camera-local time, e.g. "
            "2018-01-01 or 2018-01-01T00:00:00. Must not carry a time zone or "
            "offset. Photos with an unknown date are never included."
        ),
    ),
    captured_to: datetime.datetime | None = Query(
        default=None,
        description=(
            "Only photos taken strictly before this camera-local time; "
            "2019-01-01 ends the year 2018. Must not carry a time zone or "
            "offset."
        ),
    ),
    use_case: SearchImagesUseCase = Depends(get_search_images_use_case),
    locations: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> SearchResponseSchema:
    """Return the indexed images that best match `q`, most similar first.

    Synchronous `def`, not `async def`, and that is a decision rather than
    a default: everything below this line blocks. Encoding the query is
    ~90 ms of CPU for English and ~400 ms for Portuguese including
    translation (RFC-025 section 12), and the ranking is a blocking DBAPI
    call. FastAPI runs a `def` endpoint in a threadpool and an `async def`
    endpoint on the event loop, so declaring this coroutine would stall
    every concurrent request -- `/health` included -- for the length of a
    forward pass.

    `limit` carries no `ge`/`le` constraint on purpose. `MAX_SEARCH_LIMIT`
    already states that policy once, in `search_images.py`, with a comment
    saying it is application policy rather than a database bound. Copying
    `100` into this signature would create a second copy that drifts
    silently: raising the constant to 200 would leave the endpoint
    rejecting 101 with a 422 while every use-case test still passed. The
    cost is that an out-of-range limit comes back as 400 instead of 422,
    which is the more accurate status anyway -- `limit=101` is a
    well-formed request that policy refuses.

    The default is resolved here rather than left to the use case so that
    the response can echo the value actually applied. Nothing is
    duplicated by doing so: the resolved number is what gets passed to
    `execute()`, so what the client is told and what the search used
    cannot disagree.

    Neither the query text nor the embedding is logged (RFC-026 section
    16.1). Query length is enough to correlate a slow request with a long
    query; the text is a user's search history, and this is a local-first
    product whose premise is that it stays theirs. The device count is
    logged rather than the ids for the same reason -- how many disks a
    search was narrowed to is enough to explain a fast or an empty
    response.

    `device_id` repeats for several devices (`?device_id=A&device_id=B`)
    and is omitted for all of them. Omitted must mean *all*, never *none*:
    an absent parameter that produced an empty `IN ()` would turn every
    unfiltered search into zero results, so the empty case is carried as
    an empty `SearchFilters` that adds no clause at all.

    An unknown device id is not rejected. The filter restricts a
    candidate set rather than asserting that its members exist, so a
    search naming a disk the system has never seen correctly matches
    nothing (RFC-027 section 9). Whether the named disks are *connected*
    is not asked here either, and could not usefully be: search ranks what
    is indexed, and a hit on a disk in a drawer is the answer the product
    exists to give.

    Since RFC-030 every hit carries its device, its `relative_path`, and
    its `absolute_path` when the disk is plugged in. Resolving that is a
    second use case called after the search rather than a step inside it:
    where a file is has nothing to do with how well it matched, and
    `GET /images/{id}` needs the same answer with no search at all. The
    operating system is asked once per distinct disk on the page, not once
    per hit (RFC-030 section 4.2).

    `captured_from` / `captured_to` (RFC-028) are the two ends of a
    half-open range, `[from, to)`, and either may be sent alone. An
    omitted end becomes `datetime.min` or `datetime.max` *here*, so that
    `DateRange` stays a value with two required bounds instead of growing
    a `None` every consumer below would have to handle. Omitting both is
    not an unbounded range; it is no date filter at all, because
    `[min, max)` would silently drop every photo with an unknown date.

    **A bound with a zone or an offset is refused, never converted.**
    Pydantic parses `2018-01-01T00:00:00Z`, `...-03:00`, and even a bare
    Unix timestamp into an aware `datetime`, and `DateRange` rejects it
    with a `DomainError`, which the app-wide handler answers with 400 and
    a message naming the zone. Converting would have to pick a zone to
    convert *into*, and the capture date is camera-local precisely
    because no such zone exists (RFC-028 section 5). An inverted range
    is refused the same way. A value that is not a date at all is a 422
    from FastAPI, as for any malformed parameter.

    `excluded_unknown_date` is only computed when a range was sent. It is
    a second query, and an unfiltered search must not pay for it.
    """
    effective_limit = settings.top_k_results if limit is None else limit
    filters = SearchFilters(
        device_ids=frozenset(DeviceId(value) for value in device_id or ()),
        captured_between=_capture_range(captured_from, captured_to),
    )
    logger.info(
        "search requested: query_length=%d, limit=%d, devices=%d, date_range=%s",
        len(q),
        effective_limit,
        len(filters.device_ids),
        filters.captured_between is not None,
    )

    started_at = time.perf_counter()
    hits = use_case.execute(q, effective_limit, filters)
    excluded_unknown_date = use_case.count_hidden_by_unknown_date(filters)
    searched_at = time.perf_counter()
    located = locations.execute([hit.image for hit in hits])
    located_at = time.perf_counter()

    # `elapsed_ms` keeps meaning what RFC-026 measured -- the search -- and
    # the location step is logged beside it rather than folded in, so a slow
    # volume enumeration cannot hide inside a number that used to be CLIP.
    logger.info(
        "search completed: results=%d, elapsed_ms=%.0f, locate_ms=%.1f",
        len(hits),
        (searched_at - started_at) * 1000,
        (located_at - searched_at) * 1000,
    )
    return SearchResponseSchema.from_hits(
        query=q,
        limit=effective_limit,
        hits=hits,
        locations=located,
        excluded_unknown_date=excluded_unknown_date,
    )


@router.get(
    "/{image_id}",
    status_code=status.HTTP_200_OK,
    response_model=ImageSchema,
    summary="Describe one indexed image, and where it is right now",
    responses={404: {"description": "No indexed image has this id"}},
)
def get_image(
    image_id: UUID,
    use_case: GetImageDetailsUseCase = Depends(get_image_details_use_case),
) -> ImageSchema:
    """Return one image: its device, its paths, and whether the disk is here.

    **200 when the disk is unplugged** (RFC-030 section 4.2). The row
    exists and this describes the row; `device.connected: false` with
    `device.label` is the answer "it is on HD3", not an error.

    Declared after `/search`, and that order is load-bearing: routes match
    in declaration order, and `/{image_id}` would otherwise capture
    `/search` and refuse it as a malformed UUID.
    """
    return ImageSchema.from_located(use_case.execute(ImageId(image_id)))


@router.get(
    "/{image_id}/thumbnail",
    status_code=status.HTTP_200_OK,
    response_class=FileResponse,
    summary="The image's thumbnail, from the application's cache",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The thumbnail"},
        304: {"description": "The client's cached copy is current"},
        404: {
            "description": (
                "No such image, or it has no thumbnail yet -- show a "
                "placeholder rather than an error"
            )
        },
    },
)
def get_thumbnail(
    image_id: UUID,
    if_none_match: str | None = Header(default=None),
    use_case: GetThumbnailUseCase = Depends(get_thumbnail_use_case),
) -> Response:
    """Serve the thumbnail, or `304` when the client's copy is still right.

    **Served whether or not the photo's disk is plugged in.** Thumbnails live
    in the application's own cache, on a disk that is always there, and
    that is the only way a user can *see* a photo whose disk is in a drawer
    (RFC-030 section 4.1).

    **Not a `StaticFiles` mount.** A mount would serve by file name, with
    its own validators, and could not answer 404 for an id that has no
    thumbnail row or check `If-None-Match` against the photo's content hash
    before touching the disk.

    **`ETag` is the photo's content hash, and `Cache-Control` is
    `no-cache` -- not `max-age=31536000`, which RFC-030 section 7.2 as
    proposed paired with it.** That pairing reintroduces the bug the
    section was correcting. A response with a year of freshness is served
    from the browser's cache without asking the server at all (RFC 9111
    section 4.2), so the `ETag` would never be sent back and a photo
    overwritten in place -- same path, same id -- would keep its old
    thumbnail for a year, which is exactly the failure `immutable` had.
    `no-cache` stores the thumbnail and revalidates it on every use; a
    current copy costs a `304` with no body and no file read -- measured
    at 6.6 ms median in-process against PostgreSQL, against 8.6 ms for
    sending the 45 KB file (`experiments/rfc-030-file-access/
    measure_reveal_and_revalidation.log`). Most of that is the metadata
    read, so revalidation saves bytes more than time on a local API, and
    a page of fifty thumbnails pays it fifty times. That is the declared
    price of never showing a stale picture. `private` keeps a shared
    cache between the user and their own machine, should there ever be
    one, from keeping their photos.

    **A row with no content hash gets `no-store`.** Rows indexed before
    RFC-024 have nothing to validate against, and a cache entry that can
    never be revalidated is one that silently always misses.

    `If-None-Match` is parsed here because it is HTTP, and matched in the
    use case because "is my copy current" is not. Weak tags compare
    weakly, as RFC 9110 section 13.1.2 requires for this header, and `*`
    is not honoured -- no browser sends it on a `GET`.
    """
    result = use_case.execute(ImageId(image_id), _entity_tags(if_none_match))
    headers = _thumbnail_cache_headers(result.version)
    if isinstance(result, ThumbnailUnchanged):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return FileResponse(result.path, headers=headers)


@router.post(
    "/{image_id}/reveal",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Open the file manager on this machine with the image selected",
    dependencies=[
        Depends(require_local_file_actions_enabled),
        Depends(require_loopback_client),
    ],
    responses={
        403: {"description": "The caller is not on this machine"},
        404: {
            "description": (
                "No such image -- or local file actions are switched off, in "
                "which case the route answers as if it did not exist"
            )
        },
        409: {"description": "The image's disk is not connected"},
        410: {"description": "The disk is connected and the file is not on it"},
    },
)
def reveal_image(
    image_id: UUID,
    use_case: RevealImageUseCase = Depends(get_reveal_image_use_case),
) -> Response:
    """Ask Explorer to show the file, and answer once that has been launched.

    **The request carries an id and nothing else** -- no body, no path, no
    header the handler reads (RFC-030 section 5.1). The server finds the
    row, resolves where its disk is mounted now, and builds the path
    itself, so there is no client string in which to smuggle `..` or a
    UNC share.

    **`POST`, because this has a side effect in the physical world**
    (RFC-030 section 5.2). A `GET` could be prefetched by anything that
    follows links, and a page of ten results would open ten windows.

    The two guards are dependencies on the decorator, which FastAPI
    resolves before `use_case`: a refused request never opens a database
    session or enumerates a volume. See `local_file_actions.py`.

    204 means the process was launched. Explorer's exit status means
    nothing, so nothing here can say that a window actually opened.
    """
    use_case.execute(ImageId(image_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _entity_tags(header: str | None) -> frozenset[str]:
    """The opaque tags of an `If-None-Match` header, unquoted, weakness ignored."""
    if not header:
        return frozenset()
    tags: set[str] = set()
    for raw in header.split(","):
        tag = raw.strip().removeprefix("W/")
        if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
            tag = tag[1:-1]
        if tag:
            tags.add(tag)
    return frozenset(tags)


def _thumbnail_cache_headers(version: str | None) -> dict[str, str]:
    """`ETag` plus revalidate-always when there is a version; no caching when not."""
    if version is None:
        return {"Cache-Control": "no-store"}
    return {"ETag": f'"{version}"', "Cache-Control": "private, no-cache"}


def _capture_range(
    captured_from: datetime.datetime | None, captured_to: datetime.datetime | None
) -> DateRange | None:
    """Fill an omitted end with the extreme it stands for; `None` if both are."""
    if captured_from is None and captured_to is None:
        return None
    return DateRange(
        start=captured_from if captured_from is not None else datetime.datetime.min,
        end=captured_to if captured_to is not None else datetime.datetime.max,
    )
