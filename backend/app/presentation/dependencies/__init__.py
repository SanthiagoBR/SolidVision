"""Dependency injection helpers for presentation."""

from __future__ import annotations

from functools import lru_cache

from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import SessionLocal


def get_image_repository() -> ImageRepository:
    """Return a PostgreSQL-backed repository bound to a new session.

    A new session is opened per call rather than shared as a singleton,
    since a SQLAlchemy Session is not safe to reuse across concurrent
    requests. `InMemoryImageRepository` remains available in
    `app.infrastructure.persistence.in_memory_image_repository` for
    Application-layer unit tests that must not depend on a real database.
    """
    return PostgresImageRepository(SessionLocal())


@lru_cache(maxsize=1)
def get_embedding_model() -> EmbeddingModelPort:
    """Return the process-wide CLIP adapter, built on first use.

    Lazy on purpose, unlike the eager module-level singleton RFC-016
    established for `InMemoryImageRepository`. Importing this module is
    something most of the test suite does transitively (any import of
    `app.presentation.api` reaches it), and an eager
    `ClipEmbeddingModel()` at import time would be a landmine the day the
    constructor starts doing real work.

    `lru_cache` gives the load-once/reuse-forever semantics RFC-023
    section 10 requires while keeping construction itself free: the adapter
    defers both the CLIP checkpoint and the translation model until an
    actual `encode_*` call, so neither importing this module nor calling
    this provider downloads anything from Hugging Face.

    `FakeEmbeddingModel` is untouched in
    `app.infrastructure.ai.fake_embedding_model` and remains the test
    double for everything that must not load a real model.
    """
    return ClipEmbeddingModel()


def get_index_image_use_case() -> IndexImageUseCase:
    """Return an index use case composed with the shared dependencies."""
    return IndexImageUseCase(
        repository=get_image_repository(),
        embedding_model=get_embedding_model(),
    )


def get_search_images_use_case() -> SearchImagesUseCase:
    """Return a search use case composed with the shared dependencies."""
    return SearchImagesUseCase(
        repository=get_image_repository(),
        embedding_model=get_embedding_model(),
    )


__all__ = [
    "get_embedding_model",
    "get_image_repository",
    "get_index_image_use_case",
    "get_search_images_use_case",
]
