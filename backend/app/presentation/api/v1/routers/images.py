"""HTTP entry point for semantic image search (RFC-026).

The route owns nothing. It parses a query string, hands two values to
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
"""

from __future__ import annotations

import datetime
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.search_filters import SearchFilters
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import get_search_images_use_case
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

    The response says nothing about devices yet. Publishing the file's
    path, the disk it is on and whether that disk is plugged in is
    RFC-030, which owns the response shape; this RFC only gives a caller a
    way to narrow the question.

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
    elapsed_ms = (time.perf_counter() - started_at) * 1000

    logger.info("search completed: results=%d, elapsed_ms=%.0f", len(hits), elapsed_ms)
    return SearchResponseSchema.from_hits(
        query=q,
        limit=effective_limit,
        hits=hits,
        excluded_unknown_date=excluded_unknown_date,
    )


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
