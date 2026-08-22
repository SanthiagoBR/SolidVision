"""Carrier pairing an image with how well it matched one search."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.entities.image import Image


@dataclass(frozen=True)
class SearchHit:
    """One ranked result: the image that matched, and how strongly.

    The score is deliberately *not* a field on `Image`. An `Image` is the
    picture itself -- it is frozen, compares by id, and means the same
    thing to every caller. A similarity means nothing without the query
    that produced it: the same image scores differently for every search,
    and would score nothing at all if it were merely listed. Storing a
    per-query number on a per-image entity would make two `Image` values
    for the same file carry contradictory data while still comparing
    equal.

    Lives in Domain for the same reason as `IndexingRecord`: it is part of
    the `ImageRepository` port's own contract, so keeping it here leaves
    the port self-contained within its layer instead of making Domain
    depend on Application for its own return type.
    """

    image: Image
    similarity: float
    """Cosine similarity in [-1, 1], where 1 is identical direction.

    Not rescaled into [0, 1] anywhere along the way. Negative values are
    legitimate and mean the vectors point in opposing directions; folding
    them away would destroy the distinction between "unrelated" (~0) and
    "opposite" (~-1). Whether the number is comparable across queries is
    a property of the embedding space, not of this carrier.
    """


# Every `ImageRepository` implementation defines a `list()` method, which
# shadows the builtin `list` inside its class body: an annotation written
# as `-> list[SearchHit]` on a method of one of those classes resolves to
# the method, not to the type. The alias is evaluated here at module
# scope, where `list` still means `list`, and is the same object as
# `list[SearchHit]` for anything that compares annotations.
SearchHits = list[SearchHit]
