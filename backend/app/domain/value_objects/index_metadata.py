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
