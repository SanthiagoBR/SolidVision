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

import time

from fastapi import APIRouter, Depends, Query, status

from app.application.use_cases.search_images import SearchImagesUseCase
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
    product whose premise is that it stays theirs.
    """
    effective_limit = settings.top_k_results if limit is None else limit
    logger.info("search requested: query_length=%d, limit=%d", len(q), effective_limit)

    started_at = time.perf_counter()
    hits = use_case.execute(q, effective_limit)
    elapsed_ms = (time.perf_counter() - started_at) * 1000

    logger.info("search completed: results=%d, elapsed_ms=%.0f", len(hits), elapsed_ms)
    return SearchResponseSchema.from_hits(query=q, limit=effective_limit, hits=hits)
