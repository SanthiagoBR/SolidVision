"""Date already-indexed images without touching an embedding (RFC-028 section 7).

The backfill is the indexing scan with inference switched off, and it is built
that way on purpose rather than as a second mechanism with rules of its own:
the same discovered candidates, the same metadata prefetch per window, the
same `capture_date_to_write()` deciding which rows may be written. What it
removes is everything that costs -- hashing, the model, the embedding write --
so a 40,000-photo disk costs a header read per file instead of the ~5 hours a
reindex would (RFC-027 section 2.1).

It needs no embedding model and no content hasher, and takes neither. That is
the property RFC-028 section 7 promises, and its absence from this
constructor is what `tests/infrastructure/workers/test_capture_date_backfill.py`
checks the import graph for.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.application.use_cases.capture_date_plan import capture_date_to_write
from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    windowed,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.image_id import ImageId


@dataclass
class BackfillSummary:
    """What one backfill run found and did, as data (RFC-024 section 11)."""

    force: bool
    dry_run: bool
    discovered: int = 0

    written: int = 0
    """Rows given a capture date -- or, in a dry run, rows that would be."""

    written_by_source: dict[CaptureSource, int] = field(default_factory=dict)
    """`written`, split by the source that was recorded.

    The answer to "what fraction of this disk has a real date?", read off
    a run instead of a `GROUP BY` afterwards.
    """

    already_examined: int = 0
    """Rows skipped because a previous scan or backfill already looked.

    Nearly everything on a second run without `--force`: the mode is
    idempotent, and this counter is how that shows.
    """

    kept_stronger: int = 0
    """`--force` rows where the file now reads weaker than what is stored.

    Kept, not overwritten -- a file that fails to open today must not
    erase a date read yesterday. A non-zero count is worth a look: it is
    either damage to files on the disk or a regression in the reader.
    """

    not_indexed: int = 0
    """Files on disk with no row. The backfill dates rows; it never creates them."""

    not_readable: int = 0
    """Files that could not be opened now. Left unexamined, so a later run retries."""

    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)

    def format_report(self) -> str:
        mode = "force" if self.force else "only-unknown"
        verb = "Would write" if self.dry_run else "Written"
        by_source = ", ".join(
            f"{source.value}={count}"
            for source, count in sorted(
                self.written_by_source.items(), key=lambda item: item[0].value
            )
        )
        return "\n".join(
            [
                f"Capture date backfill finished ({mode}"
                f"{', dry run' if self.dry_run else ''})",
                "",
                f"Discovered:        {self.discovered:6d}",
                f"{verb + ':':<19}{self.written:6d}   ({by_source or 'none'})",
                f"Already examined:  {self.already_examined:6d}",
                f"Kept stronger:     {self.kept_stronger:6d}   "
                "(--force refused to downgrade)",
                f"Not indexed:       {self.not_indexed:6d}   (no row to date)",
                f"Not readable:      {self.not_readable:6d}   (left for a later run)",
                f"Failed:            {len(self.failures):6d}",
                f"Elapsed:           {self.elapsed_seconds:6.2f}s",
            ]
        )


class BackfillCaptureDatesUseCase:
    """Write capture dates for the indexed rows behind a stream of candidates.

    **Two modes, and the difference is the reason `NULL` and `'unknown'`
    are different values** (RFC-028 section 4.2).

    The default -- `--only-unknown` on the command line -- examines only
    rows whose `capture_source` is NULL, the ones nobody has looked at.
    Idempotent: a second run over the same disk writes nothing.

    `force=True` also re-examines rows already examined, for one situation
    only: the extraction improved -- it reads a tag it used to ignore -- so
    files marked `unknown` may now have a date, and nothing about those
    files changed for a scan to notice. It **never replaces a stored source
    with a weaker one**. `exif_original` survives a file that reads as
    `unknown` today, whether because the disk is failing or the reader
    regressed. The naive version -- re-read everything and write whatever
    came back -- is destructive in a way that passes every test run against
    healthy files; `capture_date_to_write()` is where the rule lives.
    """

    def __init__(
        self,
        repository: ImageRepository,
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
        self._metadata_prefetch_size = metadata_prefetch_size
        self._force = force
        self._dry_run = dry_run

    def execute(self, candidates: Iterable[IndexCandidate]) -> BackfillSummary:
        """Stream `candidates` in prefetch windows, writing once per window."""
        summary = BackfillSummary(force=self._force, dry_run=self._dry_run)
        started = time.perf_counter()

        for window in windowed(candidates, self._metadata_prefetch_size):
            summary.discovered += len(window)
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )
            to_write: dict[ImageId, tuple[IndexCandidate, CaptureDate]] = {}

            for candidate in window:
                metadata = existing.get(candidate.image.id)
                discovered = candidate.image.capture_date
                if metadata is None:
                    summary.not_indexed += 1
                    continue
                if discovered is None:
                    summary.not_readable += 1
                    continue

                capture = capture_date_to_write(
                    metadata.capture_source, discovered, force=self._force
                )
                if capture is not None:
                    to_write[candidate.image.id] = (candidate, capture)
                elif (
                    self._force
                    and metadata.capture_source is not None
                    and discovered.source.precedence
                    < metadata.capture_source.precedence
                ):
                    summary.kept_stronger += 1
                else:
                    summary.already_examined += 1

            self._write(to_write, summary)

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _write(
        self,
        to_write: dict[ImageId, tuple[IndexCandidate, CaptureDate]],
        summary: BackfillSummary,
    ) -> None:
        """One bulk write per window; on failure, per row, isolating the bad one."""
        if not to_write:
            return
        if self._dry_run:
            for _, capture in to_write.values():
                self._count(capture, summary)
            return

        try:
            self._repository.update_capture_date_many(
                {image_id: capture for image_id, (_, capture) in to_write.items()}
            )
        except Exception:
            for image_id, (candidate, capture) in to_write.items():
                try:
                    self._repository.update_capture_date(image_id, capture)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                self._count(capture, summary)
            return

        for _, capture in to_write.values():
            self._count(capture, summary)

    @staticmethod
    def _count(capture: CaptureDate, summary: BackfillSummary) -> None:
        summary.written += 1
        summary.written_by_source[capture.source] = (
            summary.written_by_source.get(capture.source, 0) + 1
        )
