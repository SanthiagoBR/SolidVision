"""API presentation package: the FastAPI application and its wiring."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.api.v1 import api_v1_router
from app.presentation.error_handlers import register_error_handlers
from app.presentation.routes import health_router

logger = get_logger(__name__)

# The string encoded at startup when warm-up is enabled, and its content
# is load-bearing in a way that is easy to miss.
#
# RFC-025 section 12 recorded two cold starts, not one: ~4.95 s for the
# CLIP checkpoint and a further ~4.20 s for the Marian translator on the
# first *Portuguese* query. An English warm-up string would close the
# first and leave the second, so the first Brazilian user of a fresh
# process would still wait four seconds. A Portuguese sentence walks the
# whole path -- detect, translate, template, encode -- and loads both.
#
# It is a full sentence for the reason RFC-023 section 7.1 measured:
# `langdetect` needs several words to be reliable, and calls `fazenda`
# Turkish. A short Portuguese string would be read as some other language,
# skip translation, and warm only CLIP while looking like it warmed both.
_WARM_UP_QUERY = "uma fotografia de teste para aquecer o modelo de busca"


def _warm_up_models() -> None:
    """Load and exercise the embedding model before the first request.

    Two problems close at once here (RFC-026 sections 9.1 and 10). The
    obvious one is latency: the first query of a process otherwise pays
    ~4.95 s to load the CLIP checkpoint, which blows the one-second
    target on its own, and the first Portuguese query pays ~4.20 s more
    for the translator. Both are loaded here, which is what
    `_WARM_UP_QUERY` being a Portuguese sentence is for.

    The subtler one is a race that only exists because the search
    endpoint is a `def` and therefore runs in a threadpool. The adapter's
    lazy load is a check-then-act on `self._model is None`, so two
    requests arriving inside the first few seconds of a cold process can
    both find it unloaded and both load the checkpoint. `lru_cache` on
    the provider does not help: it makes them share the adapter, and the
    race is inside the adapter. Loading before the server accepts
    anything means no request ever observes the unloaded state.

    A failure is logged and swallowed rather than allowed to abort
    startup. Refusing to start would take `/health` down with search --
    the probe an operator would use to diagnose exactly this -- and turn
    a transient Hugging Face outage into a crash loop. The lazy path
    remains: the first query retries the load and fails visibly if it
    still cannot.
    """
    from app.presentation.dependencies import get_embedding_model

    logger.info("Warming up the embedding model")
    started_at = time.perf_counter()
    try:
        get_embedding_model().encode_text(_WARM_UP_QUERY)
    except Exception:
        logger.warning(
            "Model warm-up failed; the first query will retry", exc_info=True
        )
        return
    elapsed = time.perf_counter() - started_at
    logger.info("Embedding model warmed up in %.2fs", elapsed)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup and shutdown work around the served lifetime of `app`.

    `lifespan` rather than `@app.on_event("startup")`, which current
    FastAPI deprecates; this is the project's first startup hook, so
    there is no existing style to match.

    Warm-up is opt-in and defaults to off (`settings.warm_up_models`).
    That is not timidity about a config flag: `TestClient(app)` used as a
    context manager -- which `tests/presentation/test_health.py` does --
    runs exactly this function, so an unconditional warm-up would
    download and load 600 MB of checkpoint during a plain `pytest`, in a
    test file that has nothing to do with search, for a route that never
    touches the model.
    """
    if settings.warm_up_models:
        _warm_up_models()
    else:
        logger.info("Model warm-up disabled; the first search will load the model")
    yield


app = FastAPI(
    title=settings.project_name,
    description="Semantic image search API for SolidVision",
    version=settings.project_version,
    lifespan=lifespan,
)

logger.info("FastAPI application initialized")
register_error_handlers(app)
app.include_router(health_router)
app.include_router(api_v1_router)

__all__ = ["app"]
