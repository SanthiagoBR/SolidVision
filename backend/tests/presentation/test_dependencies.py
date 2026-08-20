from __future__ import annotations

from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
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


def test_production_wiring_uses_the_real_clip_adapter() -> None:
    """RFC-023: production encodes real pixels, not a hash of the path."""
    model = get_embedding_model()

    assert isinstance(model, ClipEmbeddingModel)
    assert isinstance(model, EmbeddingModelPort)


def test_the_embedding_model_provider_is_lazily_cached() -> None:
    """Load-once semantics without paying anything at import time.

    RFC-016's eager module-level singleton is not usable here: this module
    is imported transitively by most of the suite, so construction must be
    deferred to first call. `lru_cache` is what provides both halves.
    """
    assert hasattr(get_embedding_model, "cache_info")
    assert get_embedding_model.cache_info().maxsize == 1


def test_resolving_the_embedding_model_downloads_nothing() -> None:
    """The fast suite must never reach Hugging Face (RFC-023 section 15).

    Constructing the adapter is deliberately free -- both the CLIP
    checkpoint and the translation model load on first `encode_*`, not in
    `__init__` -- so simply asking the container for the model, as this
    whole test module does, cannot trigger a download.
    """
    model = get_embedding_model()
    assert isinstance(model, ClipEmbeddingModel)

    assert model._model is None
    assert model._processor is None


def test_dependency_providers_compose_use_cases() -> None:
    assert isinstance(get_index_image_use_case(), IndexImageUseCase)
    assert isinstance(get_search_images_use_case(), SearchImagesUseCase)


def test_use_cases_are_composed_with_a_postgres_backed_repository() -> None:
    index_use_case = get_index_image_use_case()
    search_use_case = get_search_images_use_case()

    assert isinstance(index_use_case._repository, PostgresImageRepository)
    assert isinstance(search_use_case._repository, PostgresImageRepository)
