"""Whether a freshly read position should be written over the stored one (RFC-032).

The scan reads a position for every file it discovers, including the ones the
incremental check skips. Written back unconditionally, that is one `UPDATE`
per file per scan -- the property RFC-028 section 6 protected for capture
dates, lost again for positions. This function is the condition, shared by
the two callers that need it -- the indexing scan and the EXIF backfill -- so
they cannot come to disagree about which rows a position may be written to.

**A separate function from `capture_date_to_write()`, on purpose** (RFC-032
section 8). The two facts come out of the same header read, and a single
function deciding both would be one careless line away from writing a
position because the date needed writing, or from letting a file that lost
its date downgrade its position. Each fact has its own chain, its own stored
source, and its own decision.

And apart from `plan_indexing()`, for the reason the capture-date plan is:
that function decides whether to *re-embed*, and a position must never
influence that.
"""

from __future__ import annotations

from app.domain.value_objects.position import PositionReading
from app.domain.value_objects.position_source import PositionSource


def position_to_write(
    stored: PositionSource | None,
    discovered: PositionReading | None,
    force: bool = False,
) -> PositionReading | None:
    """Return the position to write for one row, or `None` to write nothing.

    `stored` is the row's current `position_source`, as the metadata
    prefetch read it; `discovered` is what the scan just read from the file.

    **Normal mode -- every indexing scan, and the backfill by default.**
    Write only when the row has never been examined (`stored is None`).
    After the first scan that sees a file, a re-scan writes nothing for it.
    Safe rather than lazy: if the GPS block changed, the bytes changed, the
    content hash changed, and the row took the `EMBED` path, which rewrites
    the whole row from the entity.

    **Force mode -- the backfill's `--force` only.** Also re-examine rows
    already examined, and write the new reading when it is at least as
    strong as the stored source. Never weaker: an `exif_gps` survives a file
    that reads `unknown` today, because the disk is failing or the reader
    regressed (RFC-032 section 8.1). Rewriting `unknown` with `unknown` is
    skipped as the no-op it is.

    `discovered is None` -- not examined, because `extract_gps` is off or
    the file could not be read -- never writes, in either mode. Writing it
    would record "examined" for a file nobody read.
    """
    if discovered is None:
        return None
    if stored is None:
        return discovered
    if not force:
        return None
    if discovered.source.precedence < stored.precedence:
        return None
    if discovered.source is PositionSource.UNKNOWN:
        return None
    return discovered
