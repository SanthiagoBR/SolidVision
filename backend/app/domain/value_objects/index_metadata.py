"""Carrier for previously persisted filesystem metadata."""

from __future__ import annotations

import datetime
from dataclasses import dataclass


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
