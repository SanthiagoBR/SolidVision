"""Response shapes for `GET /api/v1/images/search` (RFC-026 section 5.2, RFC-030).

The only place in the codebase allowed to decide what a search result
looks like on the wire, and the decisions it makes are mostly about what
to leave out.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from app.application.use_cases.resolve_image_location import LocatedImage
from app.domain.value_objects.search_hit import SearchHit
from app.presentation.schemas.image_schema import ImageSchema, image_fields


class SearchResultSchema(ImageSchema):
    """One ranked image, as a client sees it: the image, plus how well it matched.

    **The path is published now, and RFC-030 section 2 is why.** RFC-026
    kept it out with two arguments, recorded here rather than erased:

    > *"Publishing it would leak the server's filesystem layout to every
    > caller, and would hand clients an identifier that changes whenever a
    > file moves."*

    The first dissolved. In a local-first deployment the server's
    filesystem *is* the user's, and a search that ends in a filename and a
    UUID has found a photo without telling anyone where it is -- the last
    step of the product was missing. The second still holds, and it shapes
    what is published: `id` remains the only identifier, and the paths are
    display. No route accepts a path back.

    The shape is `ImageSchema` -- the body of `GET /images/{id}`, which
    this docstring once promised "RFC-027" would add -- plus `similarity`,
    which exists only inside a query (RFC-025 section 4.1).

    **A hit on a disconnected disk is a full answer.** `device.connected`
    is false, `absolute_path` is null, and `device.label` and
    `relative_path` say "HD3, fotos/2018/junho" -- which, for someone with
    twenty disks, is the expensive part of the question (RFC-030 section
    4.1).

    `captured_at` and `capture_source` are attributes of the photograph,
    identical for every caller and every query, and the two things that
    make a date-filtered result legible (RFC-028 section 12).
    """

    similarity: float = Field(
        description="Cosine similarity in [-1, 1]; 1 is identical direction"
    )

    @classmethod
    def from_hit(cls, hit: SearchHit, located: LocatedImage) -> SearchResultSchema:
        """Map one domain hit and its resolved location to the wire, adding nothing.

        `located` must describe `hit.image`; the pairing is checked, because
        a mismatch would publish one photo's score under another photo's
        path, and nothing downstream could notice.

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
        if located.image.id != hit.image.id:
            raise ValueError(
                f"Location for {located.image.id} paired with hit {hit.image.id}"
            )
        return cls(similarity=hit.similarity, **image_fields(located))


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
        hits: Sequence[SearchHit],
        locations: Sequence[LocatedImage],
        excluded_unknown_date: int | None = None,
    ) -> SearchResponseSchema:
        """Wrap a ranking without reordering, filtering, or truncating it.

        The order is the repository's, which is PostgreSQL's, which is the
        one the vector index produced. Re-sorting here by `similarity`
        would at best reproduce it and at worst disagree with it about
        ties, which the repository breaks by id on purpose.

        `locations` is `ResolveImageLocationUseCase`'s answer for the same
        hits, in the same order -- it keeps order by contract, so the two
        are paired positionally and `strict=True` refuses a length mismatch
        rather than dropping the tail of the ranking.
        """
        return cls(
            query=query,
            limit=limit,
            results=[
                SearchResultSchema.from_hit(hit, located)
                for hit, located in zip(hits, locations, strict=True)
            ],
            excluded_unknown_date=excluded_unknown_date,
        )
