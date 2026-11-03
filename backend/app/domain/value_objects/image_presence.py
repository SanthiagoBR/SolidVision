"""Whether the rows of a device are still on its disk (RFC-046).

A row is **missing** when the last whole-disk scan did not list its file:
`images.missing_since` holds the date of that scan, and every read that serves
the interface hides the row. The row, its vector and its thumbnail stay, so
that the next scan that lists the file again unmarks it without inference
(RFC-046 section 3). Only the purge deletes, by a button.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass

from app.domain.value_objects.image_id import ImageId


@dataclass(frozen=True, slots=True)
class ImagePresence:
    """One row of a device, as the sweep needs it (RFC-046 section 5.5).

    The id, which the sweep compares with the ids of the listing; the
    device-relative path, posix as stored, which it compares with the folders
    that could not be read; and whether the row is marked already.

    `slots`, for the reason `ChangeSignals` has them: one object per row, and
    the user's largest disk has 116,396 rows.
    """

    image_id: ImageId
    relative_path: str
    missing: bool


Presences = Iterator[ImagePresence]
"""What `list_presence()` returns: one per row of the device, in no order.

**A stream, not a list.** The sweep looks at each row once, and a list of the
116,396 rows of the user's largest disk -- each with its id and its path --
was most of the 78.7 MB the first measurement found, against a criterion of
50 (RFC-046 section 10). An alias, as `ListedImages` is.
"""


@dataclass(frozen=True)
class MissingImages:
    """How many rows of a device are marked, and since when (RFC-046 section 5.8).

    `since` is the oldest mark: the card says *"312 fotos não encontradas no
    scan de 12/03"*, and the scan that first missed any of them is the date
    that sentence is true from.
    """

    count: int
    since: datetime.datetime


@dataclass(frozen=True)
class PurgedImages:
    """What a purge deleted: how many rows, and the thumbnails they pointed at.

    `count` includes the rows that had no thumbnail; `thumbnail_paths` are the
    store's locations of the ones that had, for the store to delete after the
    rows are gone (RFC-046 section 5.7).
    """

    count: int
    thumbnail_paths: tuple[str, ...]
