"""Dependency injection helpers for presentation.

The composition root: the one module allowed to know every concrete class
at once. It imports SQLAlchemy and the CLIP adapter on purpose -- that is
what composing Infrastructure means -- and it is deliberately exempt from
the import boundary `tests/test_ai_layer_boundaries.py` enforces over
`app/presentation/api/`, which covers the routers, not the wiring.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.application.use_cases.cancel_indexing_job import CancelIndexingJobUseCase
from app.application.use_cases.create_indexing_job import CreateIndexingJobUseCase
from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_indexing_job import GetIndexingJobUseCase
from app.application.use_cases.get_thumbnail import GetThumbnailUseCase
from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.list_indexing_jobs import ListIndexingJobsUseCase
from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.reveal_image import RevealImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.file_revealer import WindowsFileRevealer
from app.infrastructure.filesystem.mounted_device_locator import MountedDeviceLocator
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from app.infrastructure.filesystem.volume_identity_provider import (
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.persistence.postgres_device_repository import (
    PostgresDeviceRepository,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.postgres_indexing_job_repository import (
    PostgresIndexingJobRepository,
)
from app.infrastructure.persistence.session import get_db


def get_image_repository(
    session: Session = Depends(get_db),
) -> ImageRepository:
    """Return a PostgreSQL-backed repository bound to the request's session.

    The session arrives from `get_db`, which opens one and closes it in a
    `finally`, so FastAPI releases the connection when the response is
    sent. Until RFC-026 this function called `SessionLocal()` itself and
    nothing ever closed the result -- harmless while the only callers
    were tests, and a real leak the moment every HTTP request ran it.

    What it leaked is worse than a connection count. A session that has
    executed a `SELECT` holds a pooled connection inside an open
    transaction; the default pool is 5 connections with 10 overflow, so
    requests arriving faster than the garbage collector reclaims sessions
    queue and then time out, non-deterministically. And an `idle in
    transaction` backend blocks autovacuum, which is the mechanism behind
    the failure RFC-025 section 7.3 measured from an accident: dead index
    entries made an HNSW scan return 1 of 3 live rows. The leak and that
    recall collapse are the same bug seen from two ends.

    A new repository per call is still correct -- a SQLAlchemy `Session`
    is not safe to share across concurrent requests -- and remains the
    seam tests override. `InMemoryImageRepository` stays available in
    `app.infrastructure.persistence.in_memory_image_repository` for
    Application-layer unit tests that must not touch a database.
    """
    return PostgresImageRepository(session)


@lru_cache(maxsize=1)
def get_embedding_model() -> EmbeddingModelPort:
    """Return the process-wide CLIP adapter, built on first use.

    **This provider takes no parameters, and must not grow any.**
    `IndexingWorker.main()` imports it and calls it as a plain
    zero-argument function, so adding a `Depends(...)` default would hand
    the CLI a `Depends` object where it expects a model, and it would
    fail at runtime far from the edit that caused it. Nothing is lost by
    the constraint: a zero-argument provider already works as a FastAPI
    dependency, which is how the use-case providers below consume it.

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


def get_index_image_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    embedding_model: EmbeddingModelPort = Depends(get_embedding_model),
) -> IndexImageUseCase:
    """Return an index use case composed with the shared dependencies.

    Injected rather than self-composed for the same reason as the search
    provider below: the repository must come from the request-scoped
    session, and every level of the graph should be overridable in tests.
    Indexing has no HTTP route -- the worker CLI is the only entry point
    (`AI_Context.md`: indexing is executed by the Worker, never by
    FastAPI) -- but leaving this one calling `get_image_repository()`
    directly would reintroduce the unclosed session the moment anything
    resolved it.
    """
    return IndexImageUseCase(
        repository=repository,
        embedding_model=embedding_model,
    )


def get_search_images_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    embedding_model: EmbeddingModelPort = Depends(get_embedding_model),
) -> SearchImagesUseCase:
    """Return a search use case composed with the shared dependencies.

    `settings.top_k_results` is read here and injected, never imported by
    the use case: the Application layer must not depend on Infrastructure
    configuration (`test_application_architecture.py` enforces it), so
    this is the layer that turns a setting into an argument -- the same
    arrangement `IndexOrUpdateImagesUseCase` uses for `batch_size`.
    """
    return SearchImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        default_limit=settings.top_k_results,
    )


def get_device_repository(
    session: Session = Depends(get_db),
) -> DeviceRepository:
    """Return a device repository bound to the request's session."""
    return PostgresDeviceRepository(session)


def get_indexing_job_repository(
    session: Session = Depends(get_db),
) -> IndexingJobRepository:
    """Return a job repository bound to the request's session (RFC-029)."""
    return PostgresIndexingJobRepository(session)


def get_device_locator() -> DeviceLocator:
    """Return the locator that answers "is this disk plugged in, and where".

    **Built per request, and it must not be cached.** The answer changes
    when the user pulls a cable and nothing notifies this process
    (RFC-027 section 7), so an `lru_cache` here -- of the kind
    `get_embedding_model` legitimately uses -- would hand a job a drive
    letter that now belongs to a different disk. The adapter is a thin
    shell over an enumeration, so constructing one costs nothing.

    This is also the reason the API runs as a host process rather than in
    a container: `WindowsVolumeIdentityProvider` refuses to construct off
    `win32`, and a container could not answer this question at all
    (RFC-029 section 6).
    """
    return MountedDeviceLocator(WindowsVolumeIdentityProvider())


def get_create_indexing_job_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    device_repository: DeviceRepository = Depends(get_device_repository),
    device_locator: DeviceLocator = Depends(get_device_locator),
) -> CreateIndexingJobUseCase:
    """Return the use case that validates a request to index and queues it."""
    return CreateIndexingJobUseCase(
        job_repository=job_repository,
        device_repository=device_repository,
        device_locator=device_locator,
    )


def get_cancel_indexing_job_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
) -> CancelIndexingJobUseCase:
    return CancelIndexingJobUseCase(job_repository=job_repository)


def get_indexing_job_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
) -> GetIndexingJobUseCase:
    return GetIndexingJobUseCase(job_repository=job_repository)


def get_list_indexing_jobs_use_case(
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
) -> ListIndexingJobsUseCase:
    return ListIndexingJobsUseCase(job_repository=job_repository)


def get_resolve_image_location_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    device_locator: DeviceLocator = Depends(get_device_locator),
) -> ResolveImageLocationUseCase:
    """Return the use case that says where images are right now (RFC-030).

    Per request, like the locator under it, and for the same reason: the
    answer is only true until someone pulls a cable.
    """
    return ResolveImageLocationUseCase(
        device_repository=device_repository,
        device_locator=device_locator,
    )


def get_image_details_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    location_resolver: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
) -> GetImageDetailsUseCase:
    """Return the use case behind `GET /api/v1/images/{id}`.

    The image and device repositories share the request's one session:
    FastAPI resolves `get_db` once per request however many providers
    depend on it.
    """
    return GetImageDetailsUseCase(
        repository=repository, location_resolver=location_resolver
    )


def get_thumbnail_store() -> ThumbnailStorePort:
    """Return the on-disk thumbnail cache at `settings.thumbnail_directory`.

    Constructing it touches nothing -- the directory is created on the first
    write, which the API never makes -- so a new one per request costs a
    `Path.resolve()`.
    """
    return FilesystemThumbnailStore(settings.thumbnail_directory)


def get_thumbnail_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    thumbnail_store: ThumbnailStorePort = Depends(get_thumbnail_store),
) -> GetThumbnailUseCase:
    """Return the use case behind `GET /api/v1/images/{id}/thumbnail`.

    No device locator: a thumbnail is served from the application's cache
    whether or not the photo's disk is plugged in (RFC-030 section 4.1).
    """
    return GetThumbnailUseCase(repository=repository, thumbnail_store=thumbnail_store)


def get_file_revealer() -> FileRevealerPort:
    """Return the Windows Explorer adapter, the only one RFC-030 delivers."""
    return WindowsFileRevealer()


def get_reveal_image_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    location_resolver: ResolveImageLocationUseCase = Depends(
        get_resolve_image_location_use_case
    ),
    file_revealer: FileRevealerPort = Depends(get_file_revealer),
) -> RevealImageUseCase:
    """Return the use case behind `POST /api/v1/images/{id}/reveal`.

    Only ever resolved after both guards passed: the route declares them on
    its decorator, which FastAPI resolves before this.
    """
    return RevealImageUseCase(
        repository=repository,
        location_resolver=location_resolver,
        file_revealer=file_revealer,
    )


__all__ = [
    "get_cancel_indexing_job_use_case",
    "get_create_indexing_job_use_case",
    "get_device_locator",
    "get_device_repository",
    "get_embedding_model",
    "get_file_revealer",
    "get_image_details_use_case",
    "get_image_repository",
    "get_index_image_use_case",
    "get_indexing_job_repository",
    "get_indexing_job_use_case",
    "get_list_indexing_jobs_use_case",
    "get_resolve_image_location_use_case",
    "get_reveal_image_use_case",
    "get_search_images_use_case",
    "get_thumbnail_store",
    "get_thumbnail_use_case",
]
