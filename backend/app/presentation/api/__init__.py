"""API presentation package: the FastAPI application and its wiring."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.infrastructure.ai.model_runtime import ModelUnavailableError
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.api.v1 import api_v1_router
from app.presentation.error_handlers import register_error_handlers
from app.presentation.model_warm_up import SearchReadiness, SearchState, model_warm_up
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


def _warm_up_models() -> SearchReadiness:
    """Load and exercise the embedding model before the first search.

    Latency is why (RFC-026 section 10): the first query of a process
    otherwise pays ~4.95 s to load the CLIP checkpoint, which blows the
    one-second target on its own, and the first Portuguese query pays
    ~4.20 s more for the translator. Both are loaded here, which is what
    `_WARM_UP_QUERY` being a Portuguese sentence is for.

    **It runs in a thread, beside the open port** (RFC-036 section 5.3).
    It used to run before uvicorn opened the port, which also kept away a
    race: the search endpoint is a `def`, two first requests run in two
    threads, and both could find the adapter unloaded. The adapters now load
    once under a lock, and a search that arrives during the warm-up waits for
    the load in progress. Waiting is the declared cost; the gain is every
    route that needs no model answering in the time of an import without
    `torch`, instead of after it and both loads.

    The models are checked first for presence and size, which is cheap
    and finds a broken installation now rather than on the first search
    (RFC-035 section 6.1); the load then checks the hashes.

    A failure is logged and reported, never raised: the API stays up.
    Refusing to start would take `/health` down with search -- the probe an
    operator would use to diagnose exactly this -- and hide the devices,
    folders, map and thumbnails, none of which needs a model (RFC-035
    section 6.5). The lazy path remains: every search retries the load, and
    answers 503 with the same line while it still cannot.
    """
    from app.infrastructure.ai.model_runtime import default_runtime
    from app.presentation.dependencies import get_embedding_model

    logger.info("Warming up the embedding model")
    started_at = time.perf_counter()
    try:
        default_runtime().check_presence()
        get_embedding_model().encode_text(_WARM_UP_QUERY)
    except ModelUnavailableError as error:
        logger.error("Model warm-up failed, and search answers 503: %s", error)
        return SearchReadiness(SearchState.UNAVAILABLE, str(error))
    except Exception:
        logger.warning(
            "Model warm-up failed; the first query will retry", exc_info=True
        )
        return SearchReadiness(SearchState.ON_DEMAND)
    elapsed = time.perf_counter() - started_at
    logger.info("Embedding model warmed up in %.2fs", elapsed)
    return SearchReadiness(SearchState.READY)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup and shutdown work around the served lifetime of `app`.

    `lifespan` rather than `@app.on_event("startup")`, which current
    FastAPI deprecates; this is the project's first startup hook, so
    there is no existing style to match.

    It starts the warm-up and returns: uvicorn opens the port only after
    this yields, and the warm-up imports `torch` (RFC-036 section 5.3).
    `/health` reports how the warm-up is going, in `search`.

    Warm-up is `settings.warm_up_models`, whose default is the mode's: on
    installed, off in a checkout (RFC-035 section 8). Off in a checkout is
    not timidity about a config flag: `TestClient(app)` used as a context
    manager -- which `tests/presentation/test_health.py` does -- runs
    exactly this function, so an unconditional warm-up would start loading
    and hashing 924 MB of checkpoints behind a plain `pytest`, in a test file
    that has nothing to do with search, for a route that never touches the
    model.
    """
    if settings.warm_up_models:
        model_warm_up.start(_warm_up_models)
    else:
        model_warm_up.skip()
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
