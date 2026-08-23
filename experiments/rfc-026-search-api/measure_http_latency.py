"""Measure what the HTTP layer costs, and what warm-up saves (RFC-026 section 12).

Absolute search latency was already measured by RFC-025 section 12 and is
almost entirely the CLIP text tower. The number this RFC needs is the
*delta*: how much routing, dependency resolution, and JSON serialization
add on top of the same call made directly. So every query here is timed
twice -- once through `SearchImagesUseCase.execute()` and once through a
real HTTP request to a real uvicorn server on a real socket -- and the
difference is the answer.

`TestClient` is deliberately not used for the timing. It drives the ASGI
app in-process and would measure the layer with its transport removed,
which is the one part being asked about.

Startup is measured separately and twice, with `WARM_UP_MODELS` off and
on, against `ARCHITECTURE.md` section 22's 5 s target.

Everything happens inside one transaction that is always rolled back, so
the development database is left exactly as it was found.

    python experiments/rfc-026-search-api/measure_http_latency.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.search_images import SearchImagesUseCase  # noqa: E402
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel  # noqa: E402
from app.infrastructure.config.constants import (  # noqa: E402
    SUPPORTED_IMAGE_EXTENSIONS,
)
from app.infrastructure.config.settings import settings  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.infrastructure.workers.indexing_worker import IndexingWorker  # noqa: E402
from app.presentation.api import app  # noqa: E402
from app.presentation.dependencies import (  # noqa: E402
    get_embedding_model,
    get_image_repository,
)
from dataset_tools.manifest import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    load_manifest,
)
from dataset_tools.materialize import materialize  # noqa: E402

ENGLISH_QUERY = "artificial fish farming ponds in a valley"
PORTUGUESE_QUERY = "uma propriedade rural com um lago"
REPEATS = 20
HOST = "127.0.0.1"
PORT = 8123
SEARCH_URL = f"http://{HOST}:{PORT}/api/v1/images/search"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def report(label: str, samples: list[float]) -> None:
    print(
        f"  {label:<34} mean {statistics.mean(samples) * 1000:7.1f} ms"
        f"   median {statistics.median(samples) * 1000:7.1f} ms"
        f"   p95 {percentile(samples, 0.95) * 1000:7.1f} ms"
        f"   max {max(samples) * 1000:7.1f} ms"
    )


def time_repeatedly(action: Callable[[], None], repeats: int) -> list[float]:
    samples = []
    for _ in range(repeats):
        started_at = time.perf_counter()
        action()
        samples.append(time.perf_counter() - started_at)
    return samples


@contextmanager
def served() -> Iterator[None]:
    """Run the real app under uvicorn in a background thread."""
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def measure_startup() -> None:
    """Time the lifespan hook with warm-up off and on.

    Runs the real `lifespan` context manager rather than booting two
    processes: what the setting controls is entirely inside it, and a
    second interpreter would fold Python's own import time into a number
    that is supposed to be about loading a checkpoint.
    """
    print("\nStartup (ARCHITECTURE.md section 22 target: < 5 s)")
    from app.presentation.api import lifespan

    async def run_lifespan() -> float:
        started_at = time.perf_counter()
        async with lifespan(app):
            return time.perf_counter() - started_at

    original = settings.warm_up_models
    try:
        settings.warm_up_models = False
        cold = asyncio.run(run_lifespan())
        print(f"  WARM_UP_MODELS=false               {cold:6.2f} s")

        settings.warm_up_models = True
        warm = asyncio.run(run_lifespan())
        print(f"  WARM_UP_MODELS=true                {warm:6.2f} s")
    finally:
        settings.warm_up_models = original


def main() -> None:
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)

    embedding_model = ClipEmbeddingModel()

    with tempfile.TemporaryDirectory() as tmp:
        root = materialize(manifest, DEMO_CORPUS_ROOT, Path(tmp) / "demo")

        connection = EngineInstance.connect()
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")

        try:
            session.execute(delete(ImageModel))
            session.commit()

            repository = PostgresImageRepository(session)
            IndexingWorker(
                filesystem_provider=FilesystemImageProvider(
                    root, SUPPORTED_IMAGE_EXTENSIONS
                ),
                index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                    repository=repository,
                    embedding_model=embedding_model,
                    content_hasher=Sha256ContentHasher(),
                    batch_size=settings.batch_size,
                    metadata_prefetch_size=settings.metadata_prefetch_size,
                ),
            ).run()

            use_case = SearchImagesUseCase(
                repository=repository,
                embedding_model=embedding_model,
                default_limit=settings.top_k_results,
            )

            # The server shares the corpus indexed above -- otherwise the
            # HTTP path would rank a different table than the direct path
            # and the delta would be a comparison of two measurements
            # taken over different data.
            app.dependency_overrides[get_image_repository] = lambda: repository
            app.dependency_overrides[get_embedding_model] = lambda: embedding_model

            # Both models load on the first call; the delta is about the
            # HTTP layer, not about the checkpoint.
            use_case.execute(ENGLISH_QUERY, 10)
            use_case.execute(PORTUGUESE_QUERY, 10)

            with served(), httpx.Client(timeout=30.0) as http:
                http.get(SEARCH_URL, params={"q": ENGLISH_QUERY})

                print(f"\nSearch latency, warm, {REPEATS} repeats each")
                direct_en = time_repeatedly(
                    lambda: use_case.execute(ENGLISH_QUERY, 10), REPEATS
                )
                http_en = time_repeatedly(
                    lambda: http.get(
                        SEARCH_URL, params={"q": ENGLISH_QUERY, "limit": 10}
                    ).raise_for_status(),
                    REPEATS,
                )
                direct_pt = time_repeatedly(
                    lambda: use_case.execute(PORTUGUESE_QUERY, 10), REPEATS
                )
                http_pt = time_repeatedly(
                    lambda: http.get(
                        SEARCH_URL, params={"q": PORTUGUESE_QUERY, "limit": 10}
                    ).raise_for_status(),
                    REPEATS,
                )

                report("English, use case direct", direct_en)
                report("English, over HTTP", http_en)
                report("Portuguese, use case direct", direct_pt)
                report("Portuguese, over HTTP", http_pt)

                english_delta = statistics.mean(http_en) - statistics.mean(direct_en)
                portuguese_delta = statistics.mean(http_pt) - statistics.mean(direct_pt)
                print(f"\n  HTTP delta, English    {english_delta * 1000:6.1f} ms")
                print(f"  HTTP delta, Portuguese {portuguese_delta * 1000:6.1f} ms")
        finally:
            app.dependency_overrides.clear()
            session.close()
            transaction.rollback()
            connection.close()

    measure_startup()


if __name__ == "__main__":
    main()
