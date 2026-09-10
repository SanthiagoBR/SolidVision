"""Narrowing applied to a semantic search before ranking (RFC-027 section 9).

RFC-025 scoped search globally on purpose and named the condition for
changing that: *"scoping search by collection is a change to what a
collection means, and needs the table, the foreign key and the ownership
rules first."* RFC-027 delivers a table and a foreign key -- for devices,
which are a physical fact rather than a modelling choice -- so the
condition is met and the first filter can exist.

The structure, not just the filter, is the deliverable. RFC-028 adds a
capture-date range to this same object; introducing the mechanism here
with one field and extending it there with a second follows what RFC-024
did when it added `content_hash` to RFC-020's `IndexMetadata`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.value_objects.device_id import DeviceId


@dataclass(frozen=True)
class SearchFilters:
    """Which subset of the index a search is allowed to rank.

    An empty `SearchFilters()` means *no narrowing at all* and must
    produce exactly the query RFC-025 shipped -- not "an empty IN list",
    which would match nothing and turn a defaulted argument into a search
    that silently returns zero results. That is why the default is the
    empty set rather than `None`: there is one representation of "no
    filter", and it is the one every caller gets by default.

    `frozenset` rather than a list or a tuple because the field carries a
    *set* of devices: order is meaningless, duplicates are meaningless,
    and the object has to stay hashable to keep the dataclass frozen in
    the way `Image` and `ImageId` are.
    """

    device_ids: frozenset[DeviceId] = field(default_factory=frozenset)
    """Restrict results to images stored on these devices; empty means all.

    A device that is not connected is still a legitimate member. Search
    ranks what is *indexed*, and the whole product argument for devices
    (RFC-027 section 2.3) is that a hit on a disk in a drawer -- reported
    as such -- is a useful answer, not a broken one.
    """

    def is_empty(self) -> bool:
        """Return whether this filter narrows anything at all.

        Named rather than left to each repository's own `if not
        filters.device_ids`, so that RFC-028's second field cannot be
        added while one implementation keeps checking only the first.
        """
        return not self.device_ids
