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

    def execute(self, query: str, limit: int | None = None) -> list[SearchHit]:
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
        """
        if not query.strip():
            raise EmptySearchQueryError(
                "Search query cannot be empty or only whitespace."
            )

        effective_limit = self._default_limit if limit is None else limit
        self._validate_limit(effective_limit)

        embedding = self._embedding_model.encode_text(query)
        return self._repository.search_similar(embedding, effective_limit)

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
