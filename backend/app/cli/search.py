"""Semantic search from the command line, composed over `SearchImagesUseCase`.

There is no non-HTTP entry point for search today (RFC-025/RFC-026 shipped it
only as a FastAPI route). This module composes the same use case the API
uses, the way `job_runner.build_runner()` already composes it for indexing:
`PostgresImageRepository` over a fresh session, and the process-wide CLIP
adapter from `app.presentation.dependencies.get_embedding_model()`.
"""

from __future__ import annotations

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
