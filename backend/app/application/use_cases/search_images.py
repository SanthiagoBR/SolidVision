"""Application use case for semantic image search (RFC-025).

The full query path, and the layer that owns none of its mechanics:

    query text -> EmbeddingModelPort.encode_text()
               -> ImageRepository.search_similar()
               -> ranked SearchHit list

or, with a picture as the query (`execute_similar_to()`):

    query image -> EmbeddingModelPort.encode_image() -> the same two steps

or, with a photo that is already indexed (`FindSimilarImagesUseCase`, RFC-038):

    image id -> ImageRepository.get_embedding() -> the same ranking, no model

Language detection, translation, and the prompt template live inside the
embedding adapter; cosine distance, ordering, and top-K live inside the
repository. What is left here is the part that belongs to neither: deciding
what counts as a usable request, and handing the resulting vector to the
place that can rank against it.

**The request policy is functions of this module, and both use cases call
them** (RFC-038 section 4.1): the page size, the radius floor and the two
counts of excluded photos are one policy, and two copies of it would be two
answers to the same question.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmptySearchQueryError,
    ImageNotFoundError,
    ImageNotIndexedError,
    InvalidGeoCircleError,
    InvalidSearchLimitError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
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

    `min_radius_m` arrives the same way, from `settings.min_radius_m`
    (RFC-032 section 6). It is policy rather than a property of circles --
    `GeoCircle` accepts any positive radius -- and the policy is about
    aircraft: the GPS records where the drone was, and an oblique shot
    shows ground tens to hundreds of metres away (section 2.2). A circle
    smaller than that offset hides, with complete confidence and no error,
    exactly the photo being looked for. Defaulted to `0.0` -- no floor --
    so the compositions written before RFC-032 keep behaving as they did;
    the API passes the configured one.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        default_limit: int,
        min_radius_m: float = 0.0,
    ) -> None:
        validate_limit(default_limit)
        validate_min_radius(min_radius_m)
        self._repository = repository
        self._embedding_model = embedding_model
        self._default_limit = default_limit
        self._min_radius_m = min_radius_m

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

        The one exception is a circle's radius (RFC-032 section 6): a
        radius below `min_radius_m` *is* an unusable request, in the way an
        absurd page size is, and it is refused before the query is encoded.
        """
        if not query.strip():
            raise EmptySearchQueryError(
                "Search query cannot be empty or only whitespace."
            )

        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        embedding = self._embedding_model.encode_text(query)
        return self._repository.search_similar(embedding, effective_limit, filters)

    def execute_similar_to(
        self,
        image: Image,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        """Return the indexed images that look most like `image`, most similar first.

        The same search with a picture in place of words: CLIP puts images
        and text in one space, so the image's own embedding is ranked against
        the index exactly as a text embedding is. It is the stronger query
        whenever the user *has* a picture -- a photo of a print, a copy
        someone sent -- because no description carries as much of the image
        as the image does.

        `image` need not be indexed; it only has to be readable at its
        `absolute_path`, which is where the embedding adapter opens it --
        `query_image()` builds one for a file that is not. The same request
        policy as `execute()` applies -- page size, and the radius floor of
        a circle -- and the order is the repository's, untouched. If `image`
        *is* indexed, it comes back first, with a similarity of ~1: an
        honest answer, not one this layer hides. For a photo the index
        already holds, `FindSimilarImagesUseCase` is the cheaper door.
        """
        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        embedding = self._embedding_model.encode_image(image)
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
        return count_hidden_by_unknown_date(self._repository, filters)

    def count_hidden_by_unknown_position(
        self, filters: SearchFilters | None
    ) -> int | None:
        """How many searchable images a circle left out for having no position.

        RFC-032 section 6.2, in the exact mould of
        `count_hidden_by_unknown_date()`. Without it the spatial filter
        answers *"there is no photo of this place"* when the truth is
        *"1,200 photos have no coordinates and none of them was looked
        at"* -- and with the ~4% of smartphone photos, every export that
        strips EXIF, and everything indexed before RFC-032, that number
        starts out large.

        `None` -- not 0 -- when there is no circle, and then **the
        repository is not called at all**. That rule lives here and not in
        the repository, because `count_unknown_position()` answers the same
        question for the map with no circle (RFC-032 section 7) and must not
        short-circuit there.
        """
        return count_hidden_by_unknown_position(self._repository, filters)


class FindSimilarImagesUseCase:
    """Rank the index against a photo it already holds (RFC-038 section 4.1).

    *"Parecidas com esta"*, from the detail of a photo. **No model**: the
    photo's vector is the one indexing stored, read back with
    `get_embedding()` and handed to the same `search_similar()` a text query
    uses. So it works with the photo's disk in a drawer -- which is the case
    the product exists for -- with search `loading` or `unavailable`, and
    without decoding anything. `execute_similar_to()` reopens the file
    instead, and needs the disk, the decoder and a forward pass of CLIP to
    produce this same vector; it stays for the file that is *not* indexed.

    `default_limit` and `min_radius_m` arrive as for `SearchImagesUseCase`,
    from the composition root, and the request policy is the same functions.
    """

    def __init__(
        self,
        repository: ImageRepository,
        default_limit: int,
        min_radius_m: float = 0.0,
    ) -> None:
        validate_limit(default_limit)
        validate_min_radius(min_radius_m)
        self._repository = repository
        self._default_limit = default_limit
        self._min_radius_m = min_radius_m

    def execute(
        self,
        image_id: ImageId,
        limit: int | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        """The images most like `image_id`, most similar first, without it.

        The request is checked first, as `SearchImagesUseCase.execute()`
        checks it before encoding: an unusable page or circle costs no read.

        **The photo itself is taken out, and its copies stay.** It would come
        back first, with a similarity of 1, and take one of the page's places
        with the photo the user was just looking at. A copy -- the same bytes
        on another disk or in another folder -- has the same vector and
        another id, comes back near the top, and answers *"where else is this
        photo"*. One more row is asked for, so a page with the source removed
        is still full; the order is the repository's, untouched.

        No vector is `ImageNotFoundError` without a row (404), and
        `ImageNotIndexedError` with one (409): the second request succeeds
        once the photo is indexed.
        """
        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        embedding = self._repository.get_embedding(image_id)
        if embedding is None:
            if not self._repository.exists(image_id):
                raise ImageNotFoundError(f"No indexed image has the id {image_id}.")
            raise ImageNotIndexedError(
                f"The image {image_id} has no embedding yet: index its folder "
                "to compare it with others."
            )
        hits = self._repository.search_similar(embedding, effective_limit + 1, filters)
        return [hit for hit in hits if hit.image.id != image_id][:effective_limit]

    def count_hidden_by_unknown_date(self, filters: SearchFilters | None) -> int | None:
        """`SearchImagesUseCase.count_hidden_by_unknown_date()`, the same function."""
        return count_hidden_by_unknown_date(self._repository, filters)

    def count_hidden_by_unknown_position(
        self, filters: SearchFilters | None
    ) -> int | None:
        """`SearchImagesUseCase.count_hidden_by_unknown_position()`, likewise."""
        return count_hidden_by_unknown_position(self._repository, filters)


def query_image(path: Path) -> Image:
    """Wrap a file on disk as the transient `Image` an image query needs.

    The embedding adapter reads pixels from an `Image`'s `absolute_path`,
    and an `Image` needs an id and a device. This one is never persisted:
    its device is the nil UUID -- no device -- and its id is derived from
    the path only so that two runs over the same file build the same value.

    Here since RFC-038, out of `app.cli.search`: the CLI's `--image` and the
    API's `POST /images/search/by-image` build the same value, and a second
    copy would be a second idea of what a query picture is.
    """
    resolved = path.resolve()
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, f"query-image:{resolved}")),
        device_id=DeviceId(uuid.UUID(int=0)),
        relative_path=ImagePath(resolved.name),
        filename=resolved.stem,
        extension=resolved.suffix.lower().lstrip("."),
        absolute_path=ImagePath(resolved),
    )


def validated_request(
    limit: int | None,
    filters: SearchFilters | None,
    default_limit: int,
    min_radius_m: float,
) -> int:
    """The page size to use, after the checks every kind of query shares."""
    effective_limit = default_limit if limit is None else limit
    validate_limit(effective_limit)
    if filters is not None and filters.taken_within is not None:
        validate_radius(filters.taken_within, min_radius_m)
    return effective_limit


def validate_limit(limit: int) -> None:
    """Reject an unusable page size, at construction and at call time.

    Applied to the injected default as well as to the per-call value,
    so a misconfigured `top_k_results` fails when the use case is
    wired rather than on whichever search first omits `limit`.
    """
    if limit <= 0:
        raise InvalidSearchLimitError(f"Search limit must be at least 1, got {limit}.")
    if limit > MAX_SEARCH_LIMIT:
        raise InvalidSearchLimitError(
            f"Search limit must be at most {MAX_SEARCH_LIMIT}, got {limit}."
        )


def validate_min_radius(min_radius_m: float) -> None:
    """A floor below zero is a wiring mistake, refused when the use case is built."""
    if min_radius_m < 0:
        raise ValueError(f"min_radius_m must not be negative, got {min_radius_m}")


def validate_radius(circle: GeoCircle, min_radius_m: float) -> None:
    """Refuse a circle smaller than what the aircraft's offset can hide.

    The message explains the offset rather than just quoting a number,
    because the person reading it chose the radius on a map and needs
    to know why a precise-looking one is the wrong one.
    """
    if circle.radius_m < min_radius_m:
        raise InvalidGeoCircleError(
            f"A search radius of {circle.radius_m:g} m is below the minimum "
            f"of {min_radius_m:g} m. Photo coordinates record where the "
            "drone was, not the ground it photographed, which for an oblique "
            "shot can be tens to hundreds of metres away -- a smaller circle "
            "would hide the very photo you are looking for."
        )


def count_hidden_by_unknown_date(
    repository: ImageRepository, filters: SearchFilters | None
) -> int | None:
    """How many searchable images a date range left out for having no date.

    `None`, and no query, without a date range; see
    `SearchImagesUseCase.count_hidden_by_unknown_date()`.
    """
    if filters is None or filters.captured_between is None:
        return None
    return repository.count_unknown_capture_date(filters)


def count_hidden_by_unknown_position(
    repository: ImageRepository, filters: SearchFilters | None
) -> int | None:
    """How many searchable images a circle left out for having no position.

    `None`, and no query, without a circle; see
    `SearchImagesUseCase.count_hidden_by_unknown_position()`.
    """
    if filters is None or filters.taken_within is None:
        return None
    return repository.count_unknown_position(filters)
