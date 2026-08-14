"""Carrier for previously persisted filesystem metadata."""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexMetadata:
    """Filesystem metadata read back from persistence for change comparison.

    Deliberately excludes `embedding` and `Image` -- reconstructing a
    1152-dimension vector just to compare two scalars would be wasteful,
    and no such reconstruction exists elsewhere in the codebase. Lives in
    Domain because it is part of the `ImageRepository` port's contract.
    """

    file_size: int | None
    file_modified_at: datetime.datetime | None
