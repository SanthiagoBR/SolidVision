"""Carrier for previously persisted filesystem metadata."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.value_objects.capture_source import CaptureSource


@dataclass(frozen=True)
class IndexMetadata:
    """Filesystem metadata read back from persistence for change comparison.

    Deliberately excludes `embedding` and `Image` -- reconstructing a
    full embedding vector just to compare two scalars would be wasteful,
    and no such reconstruction exists elsewhere in the codebase. Lives in
    Domain because it is part of the `ImageRepository` port's contract.
    """

    file_size: int | None
    file_modified_at: datetime.datetime | None
    content_hash: str | None = None
    """Fingerprint of the file's bytes at the time it was last indexed.

    Defaults to `None`, which means *unknown*, never *matches*. Rows
    written before RFC-024 carry NULL here and the column was added
    without a backfill, so a caller comparing hashes must treat `None` as
    "cannot confirm unchanged" and fall through to re-embedding. Defaulted
    rather than required so that every construction site that predates
    content hashing keeps producing the safe answer instead of silently
    claiming a match it never checked.
    """

    capture_source: CaptureSource | None = None
    """Whether this row's capture date was ever examined, and from where.

    **NOT A CHANGE SIGNAL. `plan_indexing()` must never read it.** It
    rides along in the prefetch for a different consumer: the scan writes a
    capture date only for rows whose source is still `None` -- never
    examined -- so that re-scanning an unchanged collection costs zero
    writes after the first pass (RFC-028 section 6). The prefetch is
    already one query per window, so one more scalar in it is free, where a
    separate lookup would be the per-file round trip RFC-024 section 7.1
    removed.

    A different capture date says nothing about whether the pixels
    changed. Comparing it in the skip decision would re-embed a photo --
    the most expensive operation in the system -- because its EXIF was
    edited or the extraction improved (RFC-028 section 6.1).
    `test_indexing_plan.py` fails if that comparison is ever added.

    Only `ImageRepository.update_capture_date*()` writes this column;
    `update_index_metadata()` leaves it untouched even though it receives
    an `IndexMetadata`.
    """

    thumbnail_path: str | None = None
    """Where this row's thumbnail is stored, or `None` if it has none (RFC-030).

    **NOT A CHANGE SIGNAL. `plan_indexing()` must never read it.** It rides
    the prefetch for the same reason `capture_source` does: the thumbnail
    backfill asks "which of these rows already has one?" once per window,
    and the prefetch is that query already. A missing thumbnail says
    nothing about whether the pixels changed, and re-embedding a photo
    because its thumbnail failed to render would spend the most expensive
    operation in the system to fix the cheapest.

    The location itself rather than a flag, although the backfill only
    needs the flag. `GET /images/{id}/thumbnail` needs the location and
    the `content_hash` beside it, and a boolean here would have left the
    column with no reader at all -- the route would have had to rebuild
    the location from the id, and the stored one would have become a
    second copy of a fact free to disagree with the first.

    Only `ImageRepository.save_indexed*()` and `update_thumbnail_path*()`
    write it; `update_index_metadata()` leaves it alone.
    """

    @property
    def thumbnail_generated(self) -> bool:
        """Whether a thumbnail was ever stored for this row."""
        return self.thumbnail_path is not None
