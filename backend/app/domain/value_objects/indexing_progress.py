"""The four counters an indexing run reports while it is still running.

A Domain value object rather than the Application's `IndexingSummary`,
and the split is a layering requirement rather than taste. The progress
port that RFC-029 needs lives in `app/domain/services/`, beside
`ContentHasherPort`; a port whose signature named `IndexingSummary` would
make the Domain import the Application, which is backwards and which
nothing in `app/domain/` does today.

The shape is also the better one to depend on. `IndexingSummary` carries
timings, fallback records and per-file failures because a finished run is
reported to a human; a job row carries four integers and a flag because a
client is drawing a progress bar. Coupling the two would drag every
future summary field through the database.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexingProgress:
    """How far a run has got, as of the moment it was asked.

    Every counter only grows within one run, which is what lets a client
    poll and compare. They are *not* comparable across a resume: a
    resumed job starts counting from zero and skips everything before its
    checkpoint, so `discovered_files` after a restart is smaller than it
    was before one. RFC-029 section 10 accepts that -- the checkpoint
    saves the scan, and the honest denominator problem is section 4.12's.
    """

    discovered_files: int = 0
    processed_images: int = 0
    """Images that gained or refreshed an embedding."""

    skipped_images: int = 0
    """Images the incremental decision passed over without inference.

    Reported separately because without it a re-scan of an indexed disk
    looks stuck: 39,000 of 40,000 files skipped would render as
    `processed=1000 / discovered=40000` and a bar that never moves
    (RFC-029 section 5.1).
    """

    failed_images: int = 0

    discovery_complete: bool = False
    """Whether the scan has finished, making `discovered_files` a total.

    False for most of a job's life, and the client must render that as
    "scanning N files" rather than as a percentage. Discovery is a
    generator (RFC-021), so there is no denominator until it is exhausted,
    and counting ahead of time would mean walking the disk twice
    (RFC-029 section 4.12 of the build prompt).
    """
