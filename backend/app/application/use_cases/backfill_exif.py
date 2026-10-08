"""Give already-indexed images their EXIF facts without an embedding (RFC-032 §8.1).

**One backfill, not two.** RFC-028 section 7 shipped a capture-date backfill;
a sibling position backfill would read the same bytes of the same files in a
second pass -- two seeks per file on a spinning external disk, to fill columns
that come out of one header. This use case writes both facts from the single
read the scan already makes (`read_exif_facts()`), and supersedes
`BackfillCaptureDatesUseCase` rather than standing beside it.

It is still the indexing scan with inference switched off: the same discovered
candidates, the same metadata prefetch per window, and the same write policies
-- `capture_date_to_write()` and `position_to_write()` -- deciding which rows
may be written. What it removes is everything that costs: hashing, the model,
the embedding write.

**The two facts are judged independently, always.** Each has its own stored
source, its own decision and its own write; a file that has lost its date
today keeps its position, and the other way round (RFC-032 section 8.1).

It needs no embedding model and no content hasher, and takes neither -- the
property `tests/infrastructure/workers/test_exif_backfill.py` checks on the
import graph.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from app.application.use_cases.capture_date_plan import capture_date_to_write
from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    windowed,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.position_plan import position_to_write
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.position import PositionReading
from app.domain.value_objects.position_source import PositionSource


@dataclass
class FactTally:
    """What one backfill did about one fact -- the capture date, or the position."""

    written: int = 0
    """Rows given this fact -- or, in a dry run, rows that would be."""

    written_by_source: dict[str, int] = field(default_factory=dict)
    """`written`, split by the source recorded: "what fraction has a real one?"."""

    already_examined: int = 0
    """Rows skipped because a previous scan or backfill already looked.

    Nearly everything on a second run without `--force`: the mode is
    idempotent, and this counter is how that shows.
    """

    kept_stronger: int = 0
    """`--force` rows where the file now reads weaker than what is stored.

    Kept, not overwritten. A non-zero count is worth a look: damage to the
    files, or a regression in the reader.
    """

    def count(self, source: str) -> None:
        self.written += 1
        self.written_by_source[source] = self.written_by_source.get(source, 0) + 1


@dataclass
class ExifBackfillSummary:
    """What one backfill run found and did, as data (RFC-024 section 11)."""

    force: bool
    dry_run: bool
    discovered: int = 0
    dates: FactTally = field(default_factory=FactTally)
    positions: FactTally = field(default_factory=FactTally)

    not_indexed: int = 0
    """Files on disk with no row. The backfill fills rows; it never creates them."""

    not_readable: int = 0
    """Files that could not be opened now. Left unexamined, so a later run retries."""

    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)

    def format_report(self) -> str:
        mode = "force" if self.force else "only-unknown"
        verb = "Would write" if self.dry_run else "Written"

        def by_source(tally: FactTally) -> str:
            return (
                ", ".join(
                    f"{source}={count}"
                    for source, count in sorted(tally.written_by_source.items())
                )
                or "none"
            )

        def row(label: str, read: Callable[[FactTally], int]) -> str:
            return f"{label:<19}{read(self.dates):>8d}{read(self.positions):>10d}"

        return "\n".join(
            [
                f"EXIF backfill finished ({mode}"
                f"{', dry run' if self.dry_run else ''})",
                "",
                f"Discovered:        {self.discovered:6d}",
                f"Not indexed:       {self.not_indexed:6d}   (no row to fill)",
                f"Not readable:      {self.not_readable:6d}   (left for a later run)",
                "",
                f"{'':<19}{'date':>8}{'position':>10}",
                row(verb + ":", lambda tally: tally.written),
                row("Already examined:", lambda tally: tally.already_examined),
                row("Kept stronger:", lambda tally: tally.kept_stronger),
                "",
                f"Dates by source:     {by_source(self.dates)}",
                f"Positions by source: {by_source(self.positions)}",
                "",
                f"Failed:            {len(self.failures):6d}",
                f"Elapsed:           {self.elapsed_seconds:6.2f}s",
            ]
        )


Fact = TypeVar("Fact", CaptureDate, PositionReading)
Source = CaptureSource | PositionSource


class BackfillExifUseCase:
    """Write capture dates and positions for the indexed rows behind a scan.

    **Two modes, and the difference is the reason `NULL` and `'unknown'`
    are different values** (RFC-028 section 4.2, RFC-032 section 4.4).

    The default -- `--only-unknown` -- examines only rows whose source is
    NULL, for each fact separately. Idempotent: a second run over the same
    disk writes nothing.

    `force=True` also re-examines rows already examined, for one situation
    only: the extraction improved, so rows marked `unknown` may now have a
    value, and nothing about those files changed for a scan to notice. It
    **never replaces a stored source with a weaker one**, per fact: an
    `exif_original` survives a file whose date reads `unknown` today, and an
    `exif_gps` survives a file whose position does, each judged on its own.
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

    def execute(self, candidates: Iterable[IndexCandidate]) -> ExifBackfillSummary:
        """Stream `candidates` in prefetch windows, writing at most twice per window."""
        summary = ExifBackfillSummary(force=self._force, dry_run=self._dry_run)
        started = time.perf_counter()

        for window in windowed(candidates, self._metadata_prefetch_size):
            summary.discovered += len(window)
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )
            dates: dict[ImageId, tuple[IndexCandidate, CaptureDate]] = {}
            positions: dict[ImageId, tuple[IndexCandidate, PositionReading]] = {}

            for candidate in window:
                metadata = existing.get(candidate.image.id)
                if metadata is None:
                    summary.not_indexed += 1
                    continue
                read_date = candidate.image.capture_date
                read_position = candidate.image.position_reading
                if read_date is None and read_position is None:
                    summary.not_readable += 1
                    continue

                if read_date is not None:
                    date = capture_date_to_write(
                        metadata.capture_source, read_date, force=self._force
                    )
                    if date is not None:
                        dates[candidate.image.id] = (candidate, date)
                    else:
                        self._tally_skip(
                            summary.dates, metadata.capture_source, read_date.source
                        )

                if read_position is not None:
                    position = position_to_write(
                        metadata.position_source, read_position, force=self._force
                    )
                    if position is not None:
                        positions[candidate.image.id] = (candidate, position)
                    else:
                        self._tally_skip(
                            summary.positions,
                            metadata.position_source,
                            read_position.source,
                        )

            self._write(
                dates,
                self._repository.update_capture_date_many,
                self._repository.update_capture_date,
                summary.dates,
                summary,
            )
            self._write(
                positions,
                self._repository.update_position_many,
                self._repository.update_position,
                summary.positions,
                summary,
            )

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _tally_skip(
        self, tally: FactTally, stored: Source | None, read: Source
    ) -> None:
        """Say why a fact was not written: kept stronger, or already examined."""
        if self._force and stored is not None and read.precedence < stored.precedence:
            tally.kept_stronger += 1
        else:
            tally.already_examined += 1

    def _write(
        self,
        to_write: dict[ImageId, tuple[IndexCandidate, Fact]],
        write_many: Callable[[Mapping[ImageId, Fact]], None],
        write_one: Callable[[ImageId, Fact], None],
        tally: FactTally,
        summary: ExifBackfillSummary,
    ) -> None:
        """One bulk write per window and fact; on failure, per row.

        The same trade every bulk write in the pipeline makes (RFC-024
        section 7.2): attempt the batch, and when it fails pay the
        sequential price once, so one bad row costs one fact rather than
        the window's.
        """
        if not to_write:
            return
        if self._dry_run:
            for _, fact in to_write.values():
                tally.count(fact.source.value)
            return

        try:
            write_many({image_id: fact for image_id, (_, fact) in to_write.items()})
        except Exception:
            for image_id, (candidate, fact) in to_write.items():
                try:
                    write_one(image_id, fact)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                tally.count(fact.source.value)
            return

        for _, fact in to_write.values():
            tally.count(fact.source.value)
