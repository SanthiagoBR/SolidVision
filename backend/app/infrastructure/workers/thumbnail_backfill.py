"""Give already-indexed images a thumbnail, without the model (RFC-030 section 7.2).

    python -m app.infrastructure.workers.thumbnail_backfill --root PATH
    python -m app.infrastructure.workers.thumbnail_backfill --root PATH --force
    python -m app.infrastructure.workers.thumbnail_backfill --root PATH --dry-run

Images indexed before RFC-030 have no thumbnail, and so does any image whose
thumbnail failed to render while it was being indexed. The indexing scan
does not repair either: rendering is a full decode, and doing it for every
file a re-scan skips would turn a `stat` per file into a decode per file.
This command is where that work is paid for, once, when the operator asks.

**It never loads, imports, or constructs the embedding model**, the property
`capture_date_backfill` established and
`tests/infrastructure/workers/test_thumbnail_backfill.py` checks on the real
import graph. Pillow is imported; torch is not.

It only reads the collection. Thumbnails are written to
`settings.thumbnail_directory` and nowhere else (RFC-030 section 7.1), and
that directory is skipped by the walk even if it lies under `--root`.

The disk must be plugged in, because the pictures are on it (RFC-027
section 7). **`--root` is required and has no default**, for the reason the
indexing worker gives.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.use_cases.backfill_thumbnails import (
    BackfillThumbnailsUseCase,
    ThumbnailBackfillSummary,
)
from app.application.use_cases.index_or_update_images import IndexingFailure
from app.infrastructure.logging.logger import get_logger
from app.infrastructure.workers.indexing_worker import (
    discovered_candidates,
    register_device,
)

logger = get_logger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.infrastructure.workers.thumbnail_backfill",
        description=(
            "Render a thumbnail for every indexed image under ROOT that has "
            "none, without recomputing any embedding (RFC-030 section 7.2)."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help=(
            "Directory to read. Required, with no default, so this command can "
            "never walk a photo collection nobody named (same reasoning as the "
            "indexing worker). The disk holding it must be connected."
        ),
    )
    parser.add_argument(
        "--label",
        default="",
        help=(
            "User-facing name for the disk, used only if the volume has no "
            "label yet -- the same rule as the indexing worker."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be rendered, rendering and writing nothing.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--only-missing",
        dest="force",
        action="store_false",
        help=(
            "The default. Render only rows that have no thumbnail. Idempotent: "
            "a second run over the same disk renders nothing."
        ),
    )
    mode.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "Render every indexed row again, replacing stored thumbnails -- "
            "for after THUMBNAIL_MAX_EDGE changed. A row whose file fails to "
            "render keeps the thumbnail it had."
        ),
    )
    parser.set_defaults(force=False)
    return parser


def _log_summary(summary: ThumbnailBackfillSummary) -> None:
    for failure in summary.failures:
        logger.error(
            "Failed to render a thumbnail for %s", failure.path, exc_info=failure.error
        )
    logger.info("%s", summary.format_report())


def main() -> None:
    """Compose the backfill and run it against one explicitly named root.

    Function-local imports, as in `capture_date_backfill`, so that importing
    this module for its parser or its tests pulls in no database driver.
    `app.presentation.dependencies` is absent from the list for the reason
    it is absent there: it is where the CLIP adapter is composed.
    """
    from app.application.use_cases.thumbnail_writer import ThumbnailWriter
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.filesystem_image_provider import (
        FilesystemImageProvider,
    )
    from app.infrastructure.filesystem.thumbnail_generator import (
        PillowThumbnailGenerator,
    )
    from app.infrastructure.filesystem.thumbnail_store import (
        FilesystemThumbnailStore,
    )
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

    args = _build_arg_parser().parse_args()
    root = args.root.resolve()

    session = SessionLocal()
    try:
        device, volume = register_device(
            volume_provider=WindowsVolumeIdentityProvider(),
            device_repository=PostgresDeviceRepository(session),
            root=root,
            label=args.label,
            persist=not args.dry_run,
        )
        logger.info(
            "Backfilling thumbnails under %s on device %s (%s), mounted at %s, "
            "into %s",
            root,
            device.label,
            device.id,
            volume.mount_point,
            settings.thumbnail_directory,
        )
        undiscoverable: list[IndexingFailure] = []
        # No capture date is read: this command renders pictures, and an
        # EXIF read per file would be a cost with no consumer.
        provider = FilesystemImageProvider(
            root,
            settings.supported_extensions,
            extract_capture_date=False,
            excluded_directories=(settings.thumbnail_directory,),
        )
        summary = BackfillThumbnailsUseCase(
            repository=PostgresImageRepository(session),
            thumbnail_writer=ThumbnailWriter(
                generator=PillowThumbnailGenerator(),
                store=FilesystemThumbnailStore(settings.thumbnail_directory),
                max_edge=settings.thumbnail_max_edge,
            ),
            metadata_prefetch_size=settings.metadata_prefetch_size,
            force=args.force,
            dry_run=args.dry_run,
        ).execute(
            discovered_candidates(provider, device, volume.mount_point, undiscoverable)
        )
        summary.discovered += len(undiscoverable)
        summary.failures.extend(undiscoverable)
        _log_summary(summary)
    finally:
        session.close()


if __name__ == "__main__":
    main()
