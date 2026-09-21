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
from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_indexing_job import GetIndexingJobUseCase
from app.application.use_cases.get_thumbnail import GetThumbnailUseCase
from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.list_device_folders import ListDeviceFoldersUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.list_indexing_jobs import ListIndexingJobsUseCase
from app.application.use_cases.list_mounted_volumes import ListMountedVolumesUseCase
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.application.use_cases.rename_device import RenameDeviceUseCase
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
from app.domain.services.volume_catalog import VolumeCatalog
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.file_revealer import WindowsFileRevealer
from app.infrastructure.filesystem.mounted_device_locator import MountedDeviceLocator
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from app.infrastructure.filesystem.volume_catalog import MountedVolumeCatalog
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


def get_volume_catalog() -> VolumeCatalog:
    """Return the catalogue that answers "what is plugged into this machine".

    **Built per request, and it must not be cached.** The rule is
    `get_device_locator`'s, inherited word for word: the answer changes
    when the user pulls a cable and nothing notifies this process
    (RFC-027 section 7), so an `lru_cache` here -- of the kind
    `get_embedding_model` legitimately uses -- would report a disk as
    connected minutes after it left. The adapter is a shell over an
    enumeration it has not yet performed, so constructing one costs a
    reference.

    Separate from `get_device_locator()` although both wrap the same
    provider, because the two ports answer different questions: the
    locator is asked about a `Device` the system knows, and this is asked
    about the machine, including volumes that are not a device yet
    (RFC-031 section 4.2).
    """
    return MountedVolumeCatalog(WindowsVolumeIdentityProvider())


def get_describe_devices_use_case(
    image_repository: ImageRepository = Depends(get_image_repository),
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    volume_catalog: VolumeCatalog = Depends(get_volume_catalog),
) -> DescribeDevicesUseCase:
    """Return the use case that resolves what is only true now (RFC-031 section 4).

    Per request, like the catalogue under it, and for the same reason:
    the answer is only true until somebody pulls a cable.
    """
    return DescribeDevicesUseCase(
        image_repository=image_repository,
        job_repository=job_repository,
        volume_catalog=volume_catalog,
    )


def get_list_devices_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    describer: DescribeDevicesUseCase = Depends(get_describe_devices_use_case),
) -> ListDevicesUseCase:
    """Return the use case behind `GET /api/v1/devices`.

    Every repository in the graph shares the request's one session:
    FastAPI resolves `get_db` once per request however many providers
    depend on it, so the device rows, the grouped image count and the two
    job queries are one connection's work.
    """
    return ListDevicesUseCase(device_repository=device_repository, describer=describer)


def get_list_mounted_volumes_use_case(
    volume_catalog: VolumeCatalog = Depends(get_volume_catalog),
    device_repository: DeviceRepository = Depends(get_device_repository),
) -> ListMountedVolumesUseCase:
    """Return the use case behind `GET /api/v1/volumes` (RFC-031 section 5)."""
    return ListMountedVolumesUseCase(
        volume_catalog=volume_catalog, device_repository=device_repository
    )


def get_register_device_use_case(
    volume_catalog: VolumeCatalog = Depends(get_volume_catalog),
    device_repository: DeviceRepository = Depends(get_device_repository),
) -> RegisterDeviceUseCase:
    """Return the use case behind `POST /api/v1/devices` (RFC-031 section 6).

    The same class the CLI reaches through
    `indexing_worker.register_device()`, composed differently: there it
    is handed a volume the platform resolved from a `--root` path, here
    it looks an identity up in an enumeration. One merge rule, two ways
    in (RFC-031 section 4.2).
    """
    return RegisterDeviceUseCase(
        volume_catalog=volume_catalog, device_repository=device_repository
    )


def get_rename_device_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    describer: DescribeDevicesUseCase = Depends(get_describe_devices_use_case),
) -> RenameDeviceUseCase:
    """Return the use case behind `PATCH /api/v1/devices/{id}` (RFC-031 section 7).

    The describer is here to *report* the disk's state, not to require
    it: renaming succeeds with the disk in a drawer, and RFC-027 section
    2.3 says why.
    """
    return RenameDeviceUseCase(device_repository=device_repository, describer=describer)


def get_list_device_folders_use_case(
    device_repository: DeviceRepository = Depends(get_device_repository),
    image_repository: ImageRepository = Depends(get_image_repository),
    job_repository: IndexingJobRepository = Depends(get_indexing_job_repository),
    device_locator: DeviceLocator = Depends(get_device_locator),
) -> ListDeviceFoldersUseCase:
    """Return the use case behind `GET /api/v1/devices/{id}/folders`.

    The `DeviceLocator` rather than the `VolumeCatalog`, and the
    difference is the point of there being two ports: every question this
    route asks is about one device the system already knows -- where it
    is, whether this scope is on it, what is inside that scope.
    """
    return ListDeviceFoldersUseCase(
        device_repository=device_repository,
        image_repository=image_repository,
        job_repository=job_repository,
        device_locator=device_locator,
    )


__all__ = [
    "get_cancel_indexing_job_use_case",
    "get_create_indexing_job_use_case",
    "get_describe_devices_use_case",
    "get_device_locator",
    "get_device_repository",
    "get_embedding_model",
    "get_file_revealer",
    "get_image_details_use_case",
    "get_image_repository",
    "get_index_image_use_case",
    "get_indexing_job_repository",
    "get_indexing_job_use_case",
    "get_list_device_folders_use_case",
    "get_list_devices_use_case",
    "get_list_indexing_jobs_use_case",
    "get_list_mounted_volumes_use_case",
    "get_register_device_use_case",
    "get_rename_device_use_case",
    "get_resolve_image_location_use_case",
    "get_reveal_image_use_case",
    "get_search_images_use_case",
    "get_thumbnail_store",
    "get_thumbnail_use_case",
    "get_volume_catalog",
]
