"""Give already-indexed images a thumbnail, without the model (RFC-030 section 7.2).

The shape of `BackfillCaptureDatesUseCase`, and on purpose: the same
discovered candidates, the same metadata prefetch per window, the same one
bulk write per window with a per-row fallback. What differs is what a file
costs. A capture date is a header read; a thumbnail is a full decode, a
resize and an encode -- still a small fraction of the inference a reindex
would pay, and still never the model.

It needs no embedding model and no content hasher, and takes neither.
`tests/infrastructure/workers/test_thumbnail_backfill.py` checks the command's
import graph for them, as RFC-028's backfill test does.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    windowed,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId


@dataclass
class ThumbnailBackfillSummary:
    """What one thumbnail backfill found and did, as data (RFC-024 section 11)."""

    force: bool
    dry_run: bool
    discovered: int = 0

    written: int = 0
    """Rows given a thumbnail -- or, in a dry run, rows that would be."""

    already_generated: int = 0
    """Rows skipped because they already have one.

    Nearly everything on a second run without `--force`: the default mode
    is idempotent, and this counter is how that shows.
    """

    not_indexed: int = 0
    """Files on disk with no row. Rendered for nothing: rows are never created here."""

    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)
    """Files that could not be rendered or recorded.

    Left without a thumbnail, and therefore picked up again by the next run.
    """

    def format_report(self) -> str:
        mode = "force" if self.force else "missing only"
        verb = "Would render" if self.dry_run else "Rendered"
        return "\n".join(
            [
                f"Thumbnail backfill finished ({mode}"
                f"{', dry run' if self.dry_run else ''})",
                "",
                f"Discovered:         {self.discovered:6d}",
                f"{verb + ':':<20}{self.written:6d}",
                f"Already generated:  {self.already_generated:6d}",
                f"Not indexed:        {self.not_indexed:6d}   (no row to attach to)",
                f"Failed:             {len(self.failures):6d}   "
                "(left for a later run)",
                f"Elapsed:            {self.elapsed_seconds:6.2f}s",
            ]
        )


class BackfillThumbnailsUseCase:
    """Render and record thumbnails for the indexed rows behind a stream of files.

    **Two modes.** The default renders only rows with no thumbnail --
    everything indexed before RFC-030, and every image whose thumbnail
    failed during indexing. It is idempotent: a second run over the same
    disk renders nothing.

    `force=True` renders every indexed row again, replacing what is stored.
    That is for when rendering itself changed -- `thumbnail_max_edge` was
    raised, or the adapter learned to handle a format it used to get wrong
    -- and nothing about the photos did, so no scan would notice.

    Unlike the capture-date backfill's `--force`, there is no weaker result
    to refuse: a thumbnail that fails to render today is reported and the
    stored one is left in place, because the location is only written for
    a render that succeeded.
    """

    def __init__(
        self,
        repository: ImageRepository,
        thumbnail_writer: ThumbnailWriter,
        metadata_prefetch_size: int,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        if metadata_prefetch_size < 1:
            raise ValueError(
                "metadata_prefetch_size must be at least 1, "
                f"got {metadata_prefetch_size}"
            )
        self._repository = repository
        self._writer = thumbnail_writer
        self._metadata_prefetch_size = metadata_prefetch_size
        self._force = force
        self._dry_run = dry_run

    def execute(self, candidates: Iterable[IndexCandidate]) -> ThumbnailBackfillSummary:
        """Stream `candidates` in prefetch windows, writing once per window."""
        summary = ThumbnailBackfillSummary(force=self._force, dry_run=self._dry_run)
        started = time.perf_counter()

        for window in windowed(candidates, self._metadata_prefetch_size):
            summary.discovered += len(window)
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )
            locations: dict[ImageId, tuple[IndexCandidate, str]] = {}

            for candidate in window:
                metadata = existing.get(candidate.image.id)
                if metadata is None:
                    summary.not_indexed += 1
                    continue
                if metadata.thumbnail_generated and not self._force:
                    summary.already_generated += 1
                    continue
                if self._dry_run:
                    summary.written += 1
                    continue

                try:
                    location = self._writer.write(candidate.image)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                locations[candidate.image.id] = (candidate, location)

            self._record(locations, summary)

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _record(
        self,
        locations: dict[ImageId, tuple[IndexCandidate, str]],
        summary: ThumbnailBackfillSummary,
    ) -> None:
        """One bulk write per window; on failure, per row, isolating the bad one."""
        if not locations:
            return

        try:
            self._repository.update_thumbnail_path_many(
                {image_id: location for image_id, (_, location) in locations.items()}
            )
        except Exception:
            for image_id, (candidate, location) in locations.items():
                try:
                    self._repository.update_thumbnail_path(image_id, location)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                summary.written += 1
            return

        summary.written += len(locations)
