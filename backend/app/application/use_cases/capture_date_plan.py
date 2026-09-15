"""Whether a freshly read capture date should be written over the stored one.

RFC-028 reads a capture date for every file a scan discovers, including the
ones the incremental check skips. Written back unconditionally, that is one
`UPDATE` per file per scan -- 100,000 writes every time an unchanged
collection is re-scanned, destroying the property that makes a skip cheap.
This function is the condition, and it is shared by the two callers that
need it -- the indexing scan and the backfill -- so that they cannot come to
disagree about which rows a date may be written to.

Kept apart from `plan_indexing()` on purpose. That function decides whether
to *re-embed*, and nothing about a capture date may influence that
(RFC-028 section 6.1); a single function returning both answers would be
one careless comparison away from breaking it.
"""

from __future__ import annotations

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource


def capture_date_to_write(
    stored: CaptureSource | None,
    discovered: CaptureDate | None,
    force: bool = False,
) -> CaptureDate | None:
    """Return the capture date to write for one row, or `None` to write nothing.

    `stored` is the row's current `capture_source`, as the metadata
    prefetch read it; `discovered` is what the scan just read from the file.

    **Normal mode -- every indexing scan, and the backfill by default.**
    Write only when the row has never been examined (`stored is None`).
    After the first scan that sees a file, a re-scan writes nothing for it,
    whatever it finds. That is safe rather than lazy: if the EXIF changed,
    the bytes changed, the content hash changed, and the row took the
    `EMBED` path, which rewrites the whole row anyway.

    **What normal mode deliberately never revisits** is a row already
    examined as `unknown` when the *extraction* improves -- new code that
    reads a tag the old code ignored. Nothing about the file changed, so
    nothing in a scan can notice. That is the one case `force` exists for.

    **Force mode -- the backfill's `--force` only.** Also re-examine rows
    that were already examined, and write the new result when it is at
    least as strong as the stored source in the fallback chain. Never
    weaker: a file that fails to open today, or a regression in the reader,
    must not turn a stored `exif_original` into `unknown`. The naive
    implementation -- re-read everything and write what came back -- does
    exactly that, and passes every test that only re-reads healthy files.
    Rewriting `unknown` with `unknown` is skipped as the no-op it is.

    `discovered is None` -- the file was not examined, because extraction
    is switched off or the file could not be read -- never writes, in
    either mode. Writing it would record "examined" for a file nobody
    read.
    """
    if discovered is None:
        return None
    if stored is None:
        return discovered
    if not force:
        return None
    if discovered.source.precedence < stored.precedence:
        return None
    if discovered.source is CaptureSource.UNKNOWN:
        return None
    return discovered
