"""Every photo under the filters, in capture-date order (RFC-044 section 5.4).

A listing is not a search. It has no query and so no score: `SearchHit`
carries a mandatory `similarity` on purpose (RFC-025 section 4.1), and a
listing that filled it with an invented number would hand back photos as if
they had been compared. The answer is plain `Image`s, in the order of
`ListingOrder`, a page at a time.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum

from app.domain.entities.image import Image


class ListingOrder(StrEnum):
    """Which way the capture date runs. The tie-breaks never change direction.

    `NEWEST` answers "how is it now" first, the convention of every gallery;
    `OLDEST` is the same list with the date reversed -- "from the hole in the
    ground to the finished building" (RFC-044 section 3.1).

    In both, a photo with no capture date comes **after every dated one**, in
    folder order (section 3.2). It has no place on a timeline, and putting it
    first in one direction would put the undated at the top of the list.
    """

    NEWEST = "newest"
    OLDEST = "oldest"


@dataclass(frozen=True)
class ListingSummary:
    """The whole list at a glance, for the header of its first page (section 5.3).

    `total` counts every photo of the list, the undated included; `undated`
    is how many of them have no capture date -- the group under the divider.
    `oldest` and `newest` are the first and last capture dates, naive and
    camera-local like `Image.captured_at` (RFC-028 section 5), and `None`
    when no photo of the list has a date.
    """

    total: int
    undated: int
    oldest: datetime.datetime | None
    newest: datetime.datetime | None


# The alias `SearchHits` exists for: inside an `ImageRepository` class body,
# `list[Image]` would name the `list()` method, not the type.
ListedImages = list[Image]
