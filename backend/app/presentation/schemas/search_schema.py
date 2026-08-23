"""Response shapes for `GET /api/v1/images/search` (RFC-026 section 5.2).

The only place in the codebase allowed to decide what a search result
looks like on the wire, and the decisions it makes are mostly about what
to leave out.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

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
    """

    id: UUID = Field(description="Stable identifier of the matched image")
    filename: str = Field(description="Name of the file, without its extension")
    similarity: float = Field(
        description="Cosine similarity in [-1, 1]; 1 is identical direction"
    )

    @classmethod
    def from_hit(cls, hit: SearchHit) -> SearchResultSchema:
        """Map one domain hit to its wire form, adding nothing.

        `similarity` is copied through unrounded and unclamped. Rescaling
        it into [0, 1] for a progress bar would destroy the distinction
        between "unrelated" (~0) and "opposite" (~-1) that RFC-025
        section 14 kept on purpose, and this is the layer with the least
        information about what the number means: it does not know the
        score is a cosine, or that a vector was involved. A client that
        wants a percentage can compute one from the measured
        distribution.
        """
        return cls(
            id=hit.image.id.value,
            filename=hit.image.filename,
            similarity=hit.similarity,
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

    @classmethod
    def from_hits(
        cls, query: str, limit: int, hits: list[SearchHit]
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
        )
