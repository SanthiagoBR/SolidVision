from __future__ import annotations

from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.presentation.dependencies import (
    get_embedding_model,
    get_image_repository,
    get_index_image_use_case,
    get_search_images_use_case,
)


def test_get_image_repository_returns_postgres_backed_repository() -> None:
    assert isinstance(get_image_repository(), PostgresImageRepository)


def test_get_image_repository_returns_a_new_instance_per_call() -> None:
    # A SQLAlchemy Session is not safe to share across requests, so each
    # call must open its own session-bound repository instead of reusing
    # a singleton (unlike the previous InMemoryImageRepository wiring).
    assert get_image_repository() is not get_image_repository()


def test_get_embedding_model_returns_shared_instance() -> None:
    assert get_embedding_model() is get_embedding_model()


def test_dependency_providers_compose_use_cases() -> None:
    assert isinstance(get_index_image_use_case(), IndexImageUseCase)
    assert isinstance(get_search_images_use_case(), SearchImagesUseCase)


def test_use_cases_are_composed_with_a_postgres_backed_repository() -> None:
    index_use_case = get_index_image_use_case()
    search_use_case = get_search_images_use_case()

    assert isinstance(index_use_case._repository, PostgresImageRepository)
    assert isinstance(search_use_case._repository, PostgresImageRepository)
