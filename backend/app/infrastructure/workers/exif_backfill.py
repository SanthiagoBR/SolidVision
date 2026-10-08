"""Give already-indexed images their capture date and position, without the model.

    python -m app.infrastructure.workers.exif_backfill --root PATH
    python -m app.infrastructure.workers.exif_backfill --root PATH --force
    python -m app.infrastructure.workers.exif_backfill --root PATH --dry-run

**This command replaces `capture_date_backfill`** (RFC-032 section 8.1). A
position backfill beside it would read the same header of the same files in a
second pass -- two seeks per file on a spinning external disk -- to fill
columns that come out of one read. One command now writes both facts from that
read. The change of operator command is declared here and in RFC-032 rather
than hidden behind an alias nobody would ever remove.

Rows indexed before RFC-028 carry `capture_source = NULL`, and rows indexed
before RFC-032 carry `position_source = NULL`. The next ordinary indexing scan
of their disk fills both anyway, so this command is not a prerequisite for
anything. It exists for the operator who wants the facts *now* without
composing the indexing pipeline -- and for the one case no scan can ever reach:
after the extraction improves, rows already marked `unknown` need to be read
again (`--force`). Everything RFC-028 section 7 established is kept, per fact:
`--only-unknown` is the idempotent default, and `--force` never replaces a
stored source with a weaker one -- an `exif_gps` survives a file whose GPS
reads `unknown` today, and a file can lose its date and keep its position.

**It never loads, imports, or constructs the embedding model.** That is the
entire cost argument: a header read per file instead of the ~5 hours of
inference a 40,000-photo reindex would take. The property is checked against
the real import graph, in a fresh interpreter, by
`tests/infrastructure/workers/test_exif_backfill.py` -- together with the
guard of the guard, which proves the check can fail.

It only reads the collection. Nothing is written to any file (RFC-032 section
10), and no image row is created: a file with no row is reported as not
indexed and left for the indexing worker. `--dry-run` reads everything and
writes nothing, not even the device row.

The disk must be plugged in, because the facts are inside the files (RFC-027
section 7). Rows on other disks are untouched; run the command once per disk.

**`--root` is required and has no default**, for the reason the indexing
worker gives: a command that walks a photo collection must never pick one on
its own.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.use_cases.backfill_exif import (
    BackfillExifUseCase,
    ExifBackfillSummary,
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
        prog="python -m app.infrastructure.workers.exif_backfill",
        description=(
            "Read the EXIF capture date and GPS position of every indexed image "
            "under ROOT, in one read per file, and store them without "
            "recomputing any embedding (RFC-032 section 8.1)."
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
            "The default. For each fact, examine only rows that have never been "
            "examined for it (capture_source or position_source NULL). "
            "Idempotent: a second run over the same disk writes nothing. Rows "
            "already examined and found to have nothing ('unknown') are not "
            "re-read -- use --force for that."
        ),
    )
    mode.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "Also re-examine rows already examined, for when the extraction "
            "has improved. Per fact, a stored source is only ever replaced by "
            "one at least as strong: exif_original is never overwritten by "
            "exif_digitized or unknown, and exif_gps never by unknown."
        ),
    )
    parser.set_defaults(force=False)
    return parser


def _log_summary(summary: ExifBackfillSummary) -> None:
    for failure in summary.failures:
        logger.error("Failed to backfill %s", failure.path, exc_info=failure.error)
    for fact, tally in (
        ("capture date", summary.dates),
        ("position", summary.positions),
    ):
        if tally.kept_stronger:
            logger.warning(
                "%d row(s) now read a weaker %s than the one stored and were "
                "kept as they were. Check those files for damage.",
                tally.kept_stronger,
                fact,
            )
    logger.info("%s", summary.format_report())


def main() -> None:
    """Compose the backfill and run it against one explicitly named root.

    The concrete imports are function-local, as in the indexing worker, so
    that importing this module for its argument parser or its tests pulls
    in no database driver. What is deliberately absent from the list is
    `app.presentation.dependencies`, the one place the CLIP adapter is
    composed; the job executor needs it and this command must not.
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
            "Backfilling EXIF facts under %s on device %s (%s), mounted at %s",
            root,
            device.label,
            device.id,
            volume.mount_point,
        )
        undiscoverable: list[IndexingFailure] = []
        # Both extractions are always on here, whatever `extract_capture_date`
        # and `extract_gps` say: reading these two facts is the only thing
        # this command does, and they come out of one open of each file.
        provider = FilesystemImageProvider(
            root,
            settings.supported_extensions,
            extract_capture_date=True,
            extract_gps=True,
            excluded_directories=(settings.thumbnail_directory,),
        )
        summary = BackfillExifUseCase(
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
