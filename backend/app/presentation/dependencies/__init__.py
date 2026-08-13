"""Dependency injection helpers for presentation."""

from __future__ import annotations

from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import SessionLocal

_embedding_model = FakeEmbeddingModel()


def get_image_repository() -> ImageRepository:
    """Return a PostgreSQL-backed repository bound to a new session.

    A new session is opened per call rather than shared as a singleton,
    since a SQLAlchemy Session is not safe to reuse across concurrent
    requests. `InMemoryImageRepository` remains available in
    `app.infrastructure.persistence.in_memory_image_repository` for
    Application-layer unit tests that must not depend on a real database.
    """
    return PostgresImageRepository(SessionLocal())


def get_embedding_model() -> EmbeddingModelPort:
    """Return the shared fake embedding model instance."""
    return _embedding_model


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
