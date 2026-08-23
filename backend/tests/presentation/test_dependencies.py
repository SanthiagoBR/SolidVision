from __future__ import annotations

import inspect
from typing import Any

from fastapi.params import Depends
from sqlalchemy.orm import Session

from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.config.settings import settings
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import SessionLocal, get_db
from app.presentation.dependencies import (
    get_embedding_model,
    get_image_repository,
    get_index_image_use_case,
    get_search_images_use_case,
)


def declared_dependency(provider: Any, parameter: str) -> Any:
    """Return the callable a provider's parameter is injected from."""
    default = inspect.signature(provider).parameters[parameter].default
    assert isinstance(default, Depends), f"{parameter} is not injected"
    return default.dependency


def test_get_image_repository_returns_postgres_backed_repository() -> None:
    assert isinstance(get_image_repository(SessionLocal()), PostgresImageRepository)


def test_get_image_repository_returns_a_new_instance_per_call() -> None:
    # A SQLAlchemy Session is not safe to share across requests, so each
    # call must open its own session-bound repository instead of reusing
    # a singleton (unlike the previous InMemoryImageRepository wiring).
    session = SessionLocal()
    assert get_image_repository(session) is not get_image_repository(session)


def test_the_repository_session_is_injected_from_get_db() -> None:
    """RFC-026 section 7: the leak, closed at the only place that can close it.

    This provider used to call `SessionLocal()` itself, and nothing ever
    closed the result. That was invisible while the only callers were
    tests; under HTTP every request would have parked a pooled connection
    inside an open transaction until the garbage collector got to it --
    non-deterministic pool exhaustion, and an `idle in transaction`
    backend blocking the autovacuum whose absence RFC-025 section 7.3
    measured as an HNSW scan returning 1 of 3 live rows.

    `get_db` is the generator that already did this correctly and sat
    unused, with a docstring promising it to "future FastAPI
    dependencies". Asserting on the declared dependency rather than on
    behaviour is the point: nothing observable changes when this
    regresses.
    """
    assert declared_dependency(get_image_repository, "session") is get_db

    parameters = inspect.signature(get_image_repository).parameters
    assert parameters["session"].annotation in (Session, "Session")


def test_get_embedding_model_returns_shared_instance() -> None:
    assert get_embedding_model() is get_embedding_model()


def test_the_embedding_model_provider_takes_no_arguments() -> None:
    """A non-HTTP caller depends on calling this one directly.

    `IndexingWorker.main()` imports this provider and calls it as a plain
    zero-argument function. Giving it a `Depends(...)` default -- the
    natural instinct while converting the rest of the module -- would hand
    the CLI an `lru_cache`d `Depends` object in place of a model, and it
    would fail at runtime far from the edit that caused it. Nothing is
    given up by the rule: a zero-argument provider is already a valid
    FastAPI dependency, which is how the use-case providers consume it.
    """
    assert inspect.signature(get_embedding_model).parameters == {}


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
    repository = get_image_repository(SessionLocal())
    model = get_embedding_model()

    assert isinstance(get_index_image_use_case(repository, model), IndexImageUseCase)
    assert isinstance(
        get_search_images_use_case(repository, model), SearchImagesUseCase
    )


def test_use_cases_are_composed_with_a_postgres_backed_repository() -> None:
    repository = get_image_repository(SessionLocal())
    model = get_embedding_model()

    index_use_case = get_index_image_use_case(repository, model)
    search_use_case = get_search_images_use_case(repository, model)

    assert isinstance(index_use_case._repository, PostgresImageRepository)
    assert isinstance(search_use_case._repository, PostgresImageRepository)


def test_the_use_case_providers_inject_every_collaborator() -> None:
    """The whole graph is overridable, not just its root.

    `app.dependency_overrides` keys on the callable named in a `Depends`,
    so a provider that reached for `get_image_repository()` directly would
    still work and would quietly become unswappable -- which is what the
    integration tests use to substitute a transaction-scoped session and
    a fake model without touching production code.
    """
    for provider in (get_index_image_use_case, get_search_images_use_case):
        assert declared_dependency(provider, "repository") is get_image_repository
        assert declared_dependency(provider, "embedding_model") is get_embedding_model


def test_the_search_use_case_default_limit_comes_from_settings() -> None:
    """RFC-025: `top_k_results` finally has a consumer.

    It had been configurable and unused since the settings module was
    written. The composition root is the only place allowed to read it,
    which is what this pins -- a use case that reached for `settings`
    itself would pass this assertion and fail the Application-layer
    architecture test.
    """
    use_case = get_search_images_use_case(
        get_image_repository(SessionLocal()), get_embedding_model()
    )

    assert use_case._default_limit == settings.top_k_results
