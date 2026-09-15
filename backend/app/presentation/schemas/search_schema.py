"""Response shapes for `GET /api/v1/images/search` (RFC-026 section 5.2).

The only place in the codebase allowed to decide what a search result
looks like on the wire, and the decisions it makes are mostly about what
to leave out.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.search_hit import SearchHit


class SearchResultSchema(BaseModel):
    """One ranked image, as a client sees it.

    `path` is deliberately absent even though `SearchHit.image` carries
    one. Publishing it would leak the server's filesystem layout to every
    caller, and would hand clients an identifier that changes whenever a
    file moves -- RFC-024 derives the image id from the path, so a stored
    path is both unstable and unusable against the `GET /images/{id}`
    that RFC-027 will add. `id` is the identifier; `filename` is for
    display.

    **The capture date is published; the location still is not.** RFC-030
    owns the response shape for everything about *where* a file is -- its
    path, its disk, whether that disk is plugged in -- and none of that is
    here. `captured_at` and `capture_source` are a different kind of field:
    attributes of the photograph, identical for every caller and every
    query, and the two things that make a date-filtered result legible --
    why this photo matched, and how much that date is worth. RFC-028
    section 12 records the decision.
    """

    id: UUID = Field(description="Stable identifier of the matched image")
    filename: str = Field(description="Name of the file, without its extension")
    similarity: float = Field(
        description="Cosine similarity in [-1, 1]; 1 is identical direction"
    )
    captured_at: datetime.datetime | None = Field(
        description=(
            "When the photo was taken, in the camera's local time, with no "
            "time zone and no offset (e.g. 2018-07-14T15:32:05). Null when "
            "unknown."
        )
    )
    capture_source: CaptureSource | None = Field(
        description=(
            "Where captured_at came from: exif_original (the camera clock at "
            "the shot), exif_digitized (when the image was digitised), or "
            "unknown (the file carries no date). Null if the file has not "
            "been examined yet."
        )
    )

    @classmethod
    def from_hit(cls, hit: SearchHit) -> SearchResultSchema:
        """Map one domain hit to its wire form, adding nothing.

        `similarity` is copied through unrounded and unclamped. Rescaling
        it into [0, 1] for a progress bar would destroy the distinction
        between "unrelated" (~0) and "opposite" (~-1) that RFC-025
        section 14 kept on purpose, and this layer has the least
        information about what the number means: it does not know the
        score is a cosine, or that a vector was involved. A client that
        wants a percentage can compute one from the measured
        distribution.

        `captured_at` is copied through naive, and Pydantic serializes a
        naive `datetime` as ISO 8601 with no `Z` and no offset. That is the
        output RFC-028 section 5 needs, and it is pinned by a test rather
        than trusted: a `Z` here would tell every client the camera clock
        was UTC, undoing the reason the column has no zone.
        """
        return cls(
            id=hit.image.id.value,
            filename=hit.image.filename,
            similarity=hit.similarity,
            captured_at=hit.image.captured_at,
            capture_source=hit.image.capture_source,
        )


class SearchResponseSchema(BaseModel):
    """The body of a successful search.

    `query` echoes what the client sent, never what the model was asked.
    The string CLIP actually encodes is the RFC-023 template wrapped
    around a possibly translated query -- for a Portuguese input it comes
    back as `a photo of . a rural property with a lake`, leading `". "`
    and all (RFC-025 section 11.5). Publishing that would make an
    internal artifact of the translation path part of the contract, and
    would change the response the day the template changes.

    `limit` echoes the value actually used, not the one requested, which
    is usually not sent at all: without it a caller receiving three
    results cannot tell "there were only three images" from "the default
    is smaller than I assumed".
    """

    query: str = Field(description="The query exactly as the client sent it")
    limit: int = Field(description="The page size actually applied")
    results: list[SearchResultSchema] = Field(
        description="Matches, most similar first, in the repository's order"
    )
    excluded_unknown_date: int | None = Field(
        description=(
            "How many indexed images the capture-date filter left out because "
            "their date is unknown, counted over the whole index under the "
            "other filters rather than over this page. Null when no date "
            "filter was sent."
        )
    )

    @classmethod
    def from_hits(
        cls,
        query: str,
        limit: int,
        hits: list[SearchHit],
        excluded_unknown_date: int | None = None,
    ) -> SearchResponseSchema:
        """Wrap a ranking without reordering, filtering, or truncating it.

        The order is the repository's, which is PostgreSQL's, which is the
        one the vector index produced. Re-sorting here by `similarity`
        would at best reproduce it and at worst disagree with it about
        ties, which the repository breaks by id on purpose.
        """
        return cls(
            query=query,
            limit=limit,
            results=[SearchResultSchema.from_hit(hit) for hit in hits],
            excluded_unknown_date=excluded_unknown_date,
        )
