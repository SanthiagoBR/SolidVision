"""The two stored facts that say whether a file may have changed (RFC-045)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChangeSignals:
    """The size and the modification time a row recorded for its file.

    What `ImageRepository.list_change_signals()` returns for each row of a
    device: the snapshot the read-ahead thread consults to predict, before
    it reads a single byte, whether the incremental decision will ask for
    the file's bytes (RFC-045 section 5.2). The comparison itself is
    `is_unchanged()`, beside `plan_indexing()`, so the rule stays in one
    place.

    **Not `IndexMetadata`.** That one carries the content hash, the capture
    and position sources and the thumbnail location as well, because the
    prefetch window needs them; a snapshot of a whole device needs two
    scalars per row, and holds 116,396 of them at once on the user's
    largest disk. `slots` for the same reason: one object per row.

    Both fields can be `None`, for a row written before RFC-020 that never
    had them. `is_unchanged()` treats that as changed, exactly as
    `plan_indexing()` does.
    """

    file_size: int | None
    file_modified_at: datetime.datetime | None
