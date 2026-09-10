"""Filesystem indexing worker orchestrating discovery and incremental indexing.

Also the worker CLI entry point promised by `AI_Context.md`:

    python -m app.infrastructure.workers.indexing_worker --root PATH

`AI_Context.md` writes that path as `infrastructure.workers.indexing_worker`,
without the `app.` prefix. That form has never been runnable -- the package is
`app.infrastructure`, and `pyproject.toml` puts `backend/` (not `backend/app/`)
on `pythonpath` -- so the documented path was corrected to match the code
rather than the code reshaped to match the documentation (RFC-024 section 12).
"""

from __future__ import annotations

import argparse
import datetime
from collections.abc import Iterator
from pathlib import Path

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.device_identity import compute_device_id
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class IndexingWorker:
    """Discovers image files and drives incremental indexing for all of them.

    A dependency-injected orchestration component: it does not construct
    its own filesystem provider or use case, and it never touches
    SQLAlchemy, pgvector, or embedding-model internals directly.

    RFC-024 moved the per-file `try/except` that used to live in `run()`
    into the use case, because batching is where isolation is actually at
    risk -- a batch that fails as a unit has to be retried one image at a
    time, and only the component assembling batches can do that. What
    stays here is the guard around building the domain entity itself,
    which happens before a file is ever a candidate, and the logging of
    everything the run reports back.

    RFC-027 added `device` and `mount_point`, both resolved by the
    composition root *before* the scan starts rather than looked up per
    file. Resolving once is not an optimisation: if the disk were
    identified per file, a volume that went away mid-run would produce
    half a scan attributed to one device and half attributed to nothing,
    and the ids minted in the two halves would disagree about the same
    files.

    `mount_point` is passed alongside the device rather than read off it
    because a `Device` deliberately has no such field -- where a volume is
    attached is a fact about this instant, and persisting it is the defect
    RFC-027 exists to remove (RFC-027 section 4).
    """

    def __init__(
        self,
        filesystem_provider: FilesystemImageProvider,
        index_or_update_images_use_case: IndexOrUpdateImagesUseCase,
        device: Device,
        mount_point: Path,
    ) -> None:
        self._filesystem_provider = filesystem_provider
        self._index_or_update_images_use_case = index_or_update_images_use_case
        self._device = device
        self._mount_point = mount_point

    def run(self) -> IndexingSummary:
        """Discover and incrementally index every supported image file.

        Returns the run's counters and timings so a caller -- a test, a
        benchmark, a future scheduler -- can assert on them instead of
        parsing log output. Failures are logged here rather than inside the
        use case, keeping the logging factory an Infrastructure concern.
        """
        logger.info("Indexing started")

        undiscoverable: list[IndexingFailure] = []
        summary = self._index_or_update_images_use_case.execute(
            self._candidates(undiscoverable)
        )

        summary.discovered += len(undiscoverable)
        summary.failures.extend(undiscoverable)

        for fallback in summary.inference_fallbacks:
            logger.warning(
                "Inference batch of %d failed and was retried one image at a "
                "time: %s",
                len(fallback.paths),
                fallback.error,
            )

        for fallback in summary.persistence_fallbacks:
            logger.warning(
                "Bulk write of %d rows failed and degraded to per-row: %s",
                len(fallback.paths),
                fallback.error,
            )

        for failure in summary.failures:
            logger.error(
                "Failed to index %s",
                failure.path,
                exc_info=failure.error,
            )

        logger.info("%s", summary.format_report())
        return summary

    def _candidates(
        self, undiscoverable: list[IndexingFailure]
    ) -> Iterator[IndexCandidate]:
        """Stream discovered files as Application-layer candidates.

        A generator, not a list: the use case consumes it lazily, so
        discovery, hashing, inference, and persistence interleave and the
        pipeline never holds the whole collection in memory.

        Building `ImagePath`/`ImageId` can reject a path outright, and one
        such file must not end the run. Those failures are collected rather
        than raised, because raising out of a generator would close it and
        silently truncate the scan at that file. Since RFC-027 the same
        guard also covers `relative_to()`, which raises for a discovered
        file that is somehow not under the mount point the run resolved.

        Both paths are attached to the entity, and they are not
        redundant. `relative_path` is the durable half that gets persisted
        and hashed into the id; `absolute_path` is where the file happens
        to be while this disk is plugged in, so that the hasher and the
        model can open it, and it is never written to the database
        (RFC-027 section 5.2).
        """
        for discovered in self._filesystem_provider.discover():
            logger.info("Discovered file: %s", discovered.path)
            try:
                absolute_path = ImagePath(str(discovered.path))
                relative_path = ImagePath(
                    discovered.path.relative_to(self._mount_point)
                )
                image = Image(
                    id=compute_image_id(self._device.id, relative_path),
                    device_id=self._device.id,
                    relative_path=relative_path,
                    filename=discovered.filename,
                    extension=discovered.extension,
                    absolute_path=absolute_path,
                )
            except Exception as exc:
                undiscoverable.append(
                    IndexingFailure(path=str(discovered.path), error=exc)
                )
                continue

            yield IndexCandidate(
                image=image,
                file_size=discovered.file_size,
                file_modified_at=discovered.file_modified_at,
            )


def register_device(
    volume_provider: VolumeIdentityProvider,
    device_repository: DeviceRepository,
    root: Path,
    label: str,
    persist: bool = True,
) -> tuple[Device, ResolvedVolume]:
    """Identify the volume holding `root` and make sure it has a device row.

    Runs once per invocation, before a single file is discovered. That
    ordering is what RFC-027 section 14 means by "resolves the device
    before scanning", and it is not merely tidy: every `ImageId` the run
    mints is `uuid5` over `f"{device_id}/{relative_path}"`, so the device
    has to be fixed before the first id can be computed, and the foreign
    key on `images.device_id` means its row has to be committed before the
    first image is written.

    Returns the `ResolvedVolume` as well, because the run needs the mount
    point to turn discovered absolute paths into device-relative ones --
    and the mount point is exactly the thing a `Device` refuses to carry.

    A device that is already known keeps its `first_seen_at` and, unless
    `--label` was given, its label. `last_seen_at` moves every run, which
    is what makes it history rather than state.

    `persist=False` computes the device without writing it, which is what
    `device_reconcile --dry-run` needs: a dry run that created a row would
    not be a dry run, and the device id is derived rather than allocated,
    so nothing has to be written for it to be known.

    Shared with `device_reconcile` rather than written twice. The rules
    about which fields survive an existing row are the kind that drift
    apart in two copies, and the two callers must agree, because both mint
    ids from the device they produce.
    """
    volume = volume_provider.resolve(root)
    now = datetime.datetime.now(tz=datetime.UTC)
    existing = device_repository.get_by_volume_identity(volume.identity)

    device = Device(
        id=compute_device_id(volume.identity),
        volume_identity=volume.identity,
        label=(
            label
            or (existing.label if existing else None)
            or volume.filesystem_label
            or volume.identity.value
        ),
        filesystem_label=volume.filesystem_label,
        total_bytes=volume.total_bytes,
        first_seen_at=existing.first_seen_at if existing else now,
        last_seen_at=now,
        last_scan_at=existing.last_scan_at if existing else None,
        last_scan_file_count=existing.last_scan_file_count if existing else None,
    )
    if persist:
        device_repository.save(device)
    return device, volume


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.indexing_worker",
        description="Index every supported image under ROOT into PostgreSQL.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "Directory to scan. Required on purpose: there is no default, "
            "not even settings.indexing_root_path, so that running this "
            "command can never start indexing a real photo collection "
            "nobody asked it to touch (same reasoning as "
            "dataset_tools/seed_demo.py)."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        help=(
            "User-facing name for the disk ROOT lives on -- 'HD2'. Used only "
            "when this volume is being registered for the first time; a "
            "device already known keeps the label it has, because renaming a "
            "disk is a decision for the user rather than a side effect of "
            "indexing it again. Falls back to the volume's own filesystem "
            "label, which is frequently 'Untitled' or empty -- hence the "
            "option."
        ),
    )
    return parser


def main() -> None:
    """Compose the real pipeline and run it against an explicitly named root.

    This function is the composition root -- the one place allowed to know
    every concrete class at once. `IndexingWorker` itself stays injected
    and knows none of them.

    The provider imports are function-local rather than module-level so
    that importing `IndexingWorker` never drags Presentation, and through
    it the CLIP adapter, into an Infrastructure import graph. Most of the
    test suite imports this module; paying a torch import for a CLI that
    is not being run would be a real cost, not a hypothetical one.
    """
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
    from app.infrastructure.filesystem.volume_identity_provider import (
        WindowsVolumeIdentityProvider,
    )
    from app.infrastructure.persistence.postgres_device_repository import (
        PostgresDeviceRepository,
    )
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    args = _build_arg_parser().parse_args()
    root = args.root.resolve()

    session = SessionLocal()
    try:
        device, volume = register_device(
            volume_provider=WindowsVolumeIdentityProvider(),
            device_repository=PostgresDeviceRepository(session),
            root=root,
            label=args.label,
        )
        logger.info(
            "Indexing %s on device %s (%s), mounted at %s",
            root,
            device.label,
            device.id,
            volume.mount_point,
        )
        worker = IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                root, settings.supported_extensions
            ),
            index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                repository=PostgresImageRepository(session),
                embedding_model=get_embedding_model(),
                content_hasher=Sha256ContentHasher(),
                batch_size=settings.batch_size,
                metadata_prefetch_size=settings.metadata_prefetch_size,
            ),
            device=device,
            mount_point=volume.mount_point,
        )
        worker.run()
    finally:
        session.close()


if __name__ == "__main__":
    main()
