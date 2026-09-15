"""Give already-indexed images a capture date, without the model (RFC-028 section 7).

    python -m app.infrastructure.workers.capture_date_backfill --root PATH
    python -m app.infrastructure.workers.capture_date_backfill --root PATH --force
    python -m app.infrastructure.workers.capture_date_backfill --root PATH --dry-run

Rows indexed before RFC-028 carry `capture_source = NULL`. The next ordinary
indexing scan of their disk dates them anyway (RFC-028 section 6), so this
command is not a prerequisite for anything. It exists for the operator who
wants the dates *now* without composing the indexing pipeline -- and for the
one case no scan can ever reach: after the extraction improves, rows already
marked `unknown` need to be read again (`--force`).

**It never loads, imports, or constructs the embedding model.** That is the
entire cost argument of RFC-028 section 7: a header read per file instead of
the ~5 hours of inference a 40,000-photo reindex would take. The property is
checked against the real import graph by
`tests/infrastructure/workers/test_capture_date_backfill.py`, rather than
assumed from the fact that nothing here *calls* the model -- a stray import in
this module or anything it imports would load torch just the same.

It only reads the collection. Nothing is written to any file (RFC-028 section
10), and no image row is created: a file with no row is reported as not
indexed and left for the indexing worker.

The disk must be plugged in, because the dates are inside the files
(RFC-027 section 7). Rows on other disks are untouched; run the command once
per disk.

**`--root` is required and has no default**, for the reason the indexing
worker gives: a command that walks a photo collection must never pick one on
its own.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.use_cases.backfill_capture_dates import (
    BackfillCaptureDatesUseCase,
    BackfillSummary,
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
        prog="python -m app.infrastructure.workers.capture_date_backfill",
        description=(
            "Read the EXIF capture date of every indexed image under ROOT and "
            "store it, without recomputing any embedding (RFC-028 section 7)."
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
        help="Read every file and report what would be written, writing nothing.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--only-unknown",
        dest="force",
        action="store_false",
        help=(
            "The default. Examine only rows whose capture date has never been "
            "examined (capture_source NULL). Idempotent: a second run over the "
            "same disk writes nothing. Rows already examined and found to have "
            "no date ('unknown') are not re-read -- use --force for that."
        ),
    )
    mode.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "Also re-examine rows already examined, for when the extraction "
            "has improved. A stored source is only ever replaced by one at "
            "least as strong: exif_original is never overwritten by "
            "exif_digitized or unknown."
        ),
    )
    parser.set_defaults(force=False)
    return parser


def _log_summary(summary: BackfillSummary) -> None:
    for failure in summary.failures:
        logger.error("Failed to date %s", failure.path, exc_info=failure.error)
    if summary.kept_stronger:
        logger.warning(
            "%d row(s) now read weaker than their stored capture date and were "
            "kept as they were. Check those files for damage.",
            summary.kept_stronger,
        )
    logger.info("%s", summary.format_report())


def main() -> None:
    """Compose the backfill and run it against one explicitly named root.

    The concrete imports are function-local, as in the indexing worker, so
    that importing this module for its argument parser or its tests pulls
    in no database driver. What is deliberately absent from the list is
    `app.presentation.dependencies`, the one place the CLIP adapter is
    composed; the indexing worker needs it and this command must not.
    """
    from app.infrastructure.config.settings import settings
    from app.infrastructure.filesystem.filesystem_image_provider import (
        FilesystemImageProvider,
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
            "Backfilling capture dates under %s on device %s (%s), mounted at %s",
            root,
            device.label,
            device.id,
            volume.mount_point,
        )
        undiscoverable: list[IndexingFailure] = []
        # Extraction is always on here, whatever `settings.extract_capture_date`
        # says: reading the date is the only thing this command does.
        provider = FilesystemImageProvider(
            root, settings.supported_extensions, extract_capture_date=True
        )
        summary = BackfillCaptureDatesUseCase(
            repository=PostgresImageRepository(session),
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
