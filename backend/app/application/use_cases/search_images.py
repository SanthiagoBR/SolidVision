"""Application use case for semantic image search (RFC-025).

The full query path, and the layer that owns none of its mechanics:

    query text -> EmbeddingModelPort.encode_text()
               -> ImageRepository.search_similar()
               -> ranked SearchHit list

Language detection, translation, and the prompt template live inside the
embedding adapter; cosine distance, ordering, and top-K live inside the
repository. What is left here is the part that belongs to neither: deciding
what counts as a usable request, and handing the resulting vector to the
place that can rank against it.
"""

from __future__ import annotations

from app.domain.exceptions import (
    EmptySearchQueryError,
    InvalidSearchLimitError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit

# The largest page any caller may ask for. Application policy rather than
# a repository or database limit: it exists so that one request cannot ask
# a future HTTP layer to serialize an unbounded result set, and it is
# enforced by raising (see `InvalidSearchLimitError`) rather than by
# clamping, so a caller asking for more finds out.
MAX_SEARCH_LIMIT = 100


class SearchImagesUseCase:
    """Turn a text query into a ranked list of images.

    `default_limit` is injected rather than read from configuration,
    following `IndexOrUpdateImagesUseCase`'s `batch_size`: the Application
    layer must not import `settings` (it would be a dependency on
    Infrastructure, which `test_application_architecture.py` enforces), so
    the composition root passes `settings.top_k_results` in.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        default_limit: int,
    ) -> None:
        self._validate_limit(default_limit)
        self._repository = repository
        self._embedding_model = embedding_model
        self._default_limit = default_limit

    def execute(
        self,
        query: str,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        """Return the images best matching `query`, most similar first.

        The returned order is the repository's, passed through untouched.
        Re-sorting here would be both redundant and wrong: the repository
        ranked against the embeddings it stores, and this layer has no
        access to them -- sorting by `SearchHit.similarity` would at best
        reproduce that order and at worst quietly disagree with it about
        ties.

        `limit` defaults to the injected `default_limit` when omitted.
        `None` means "unspecified", never "unlimited": there is no way to
        ask for every image, because `MAX_SEARCH_LIMIT` would have to
        stop it anyway.

        `filters` is passed straight through, unvalidated, and that is
        deliberate rather than an omission. This layer owns policy about
        what counts as a usable *request* -- an empty query, an absurd
        page size -- and a device that does not exist is not one: the
        filter is a restriction on the candidate set, so naming an unknown
        device is a search that legitimately matches nothing (RFC-027
        section 9). Checking would also mean this use case acquiring a
        `DeviceRepository` it otherwise has no reason to hold, to reject
        the one case that already answers correctly.
        """
        if not query.strip():
            raise EmptySearchQueryError(
                "Search query cannot be empty or only whitespace."
            )

        effective_limit = self._default_limit if limit is None else limit
        self._validate_limit(effective_limit)

        embedding = self._embedding_model.encode_text(query)
        return self._repository.search_similar(embedding, effective_limit, filters)

    def count_hidden_by_unknown_date(self, filters: SearchFilters | None) -> int | None:
        """How many searchable images a date range left out for having no date.

        RFC-028 section 4.1 requires the UI to be able to say it: an empty
        result for "2018" must be distinguishable from "the 2018 photo is
        indexed but has no EXIF", or the product repeats the silent wrong
        answer RFC-028 section 2.1 exists to remove.

        `None` -- not 0 -- when there is no date range, and in that case the
        repository is not asked at all. The two answers mean different
        things to a client: 0 says "the date filter hid nothing", `None`
        says "there was no date filter to hide anything", and an unfiltered
        search must not pay for a second query to learn a number nobody
        requested (RFC-028 section 8).

        A separate call rather than a second return value of `execute()`:
        the count is a second query with its own cost, over a different
        universe than the ranking -- the whole table under the filters, not
        the neighbourhood an approximate index explored -- and the page
        `execute()` returns stays exactly what it was.
        """
        if filters is None or filters.captured_between is None:
            return None
        return self._repository.count_unknown_capture_date(filters)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        """Reject an unusable page size, at construction and at call time.

        Applied to the injected default as well as to the per-call value,
        so a misconfigured `top_k_results` fails when the use case is
        wired rather than on whichever search first omits `limit`.
        """
        if limit <= 0:
            raise InvalidSearchLimitError(
                f"Search limit must be at least 1, got {limit}."
            )
        if limit > MAX_SEARCH_LIMIT:
            raise InvalidSearchLimitError(
                f"Search limit must be at most {MAX_SEARCH_LIMIT}, got {limit}."
            )
