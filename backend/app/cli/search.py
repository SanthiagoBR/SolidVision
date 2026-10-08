"""Semantic search from the command line, composed over `SearchImagesUseCase`.

There is no non-HTTP entry point for search today (RFC-025/RFC-026 shipped it
only as a FastAPI route). This module composes the same use case the API
uses, the way `job_runner.build_runner()` already composes it for indexing:
`PostgresImageRepository` over a fresh session, and the process-wide CLIP
adapter from `app.presentation.dependencies.get_embedding_model()`.

Two queries: text (`run()`) and a picture (`run_image()`), which ranks the
index against the picture's own CLIP embedding.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.domain.entities.image import Image
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.search_hit import SearchHits


def format_hits(hits: SearchHits) -> str:
    """Render ranked hits as one line per result, most similar first.

    A pure function so the output format can be tested without a database
    or a model -- `run()` is the only caller that needs either.
    """
    if not hits:
        return "No results."
    return "\n".join(
        f"{rank}. {hit.similarity:.3f}  {hit.image.relative_path}  "
        f"(device {hit.image.device_id})"
        for rank, hit in enumerate(hits, start=1)
    )


def run(query: str, limit: int | None) -> None:
    """Search the index for `query` and print the ranked results.

    Function-local imports for the same reason `indexing_worker.run()` uses
    them: importing this module must not drag SQLAlchemy or the CLIP
    adapter into an import graph that a test of `app.cli.__main__`'s
    argument parsing does not need to pay for.
    """
    from app.application.use_cases.search_images import SearchImagesUseCase
    from app.infrastructure.config.settings import settings
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    session = SessionLocal()
    try:
        use_case = SearchImagesUseCase(
            repository=PostgresImageRepository(session),
            embedding_model=get_embedding_model(),
            default_limit=settings.top_k_results,
        )
        hits = use_case.execute(query, limit=limit)
    finally:
        session.close()

    print(format_hits(hits))


def query_image(path: Path) -> Image:
    """Wrap a file on disk as the transient `Image` an image query needs.

    The embedding adapter reads pixels from an `Image`'s `absolute_path`,
    and an `Image` needs an id and a device. This one is never persisted:
    its device is the nil UUID -- no device -- and its id is derived from
    the path only so that two runs over the same file build the same value.
    """
    resolved = path.resolve()
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, f"query-image:{resolved}")),
        device_id=DeviceId(uuid.UUID(int=0)),
        relative_path=ImagePath(resolved.name),
        filename=resolved.stem,
        extension=resolved.suffix.lower().lstrip("."),
        absolute_path=ImagePath(resolved),
    )


def run_image(path: Path, limit: int | None) -> None:
    """Find the indexed images that look most like the picture at `path`.

    The query is the picture itself, encoded by CLIP's image encoder, so a
    photo of a printed photo -- or any copy -- can find its original
    without anyone describing it in words. Crop away whatever is not the
    picture before querying (a table, a hand, a frame): CLIP sees the whole
    file, and the background is part of what it compares.

    A missing file is refused before the model is loaded or the database
    is opened, so the error names the path rather than a decoder.
    """
    if not path.is_file():
        raise SystemExit(f"No image file at {path}")

    from app.application.use_cases.search_images import SearchImagesUseCase
    from app.infrastructure.config.settings import settings
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    session = SessionLocal()
    try:
        use_case = SearchImagesUseCase(
            repository=PostgresImageRepository(session),
            embedding_model=get_embedding_model(),
            default_limit=settings.top_k_results,
        )
        hits = use_case.execute_similar_to(query_image(path), limit=limit)
    finally:
        session.close()

    print(format_hits(hits))
