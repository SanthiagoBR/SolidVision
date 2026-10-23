"""Every photo of a place, by capture date, with no query (RFC-044).

The use case behind *"Ver todas as fotos daqui, por data"*: the client of a
professional photographs a building site until it is finished -- months of
photos of one place -- and a search for *"construção"* ranks the finished
building by its likeness to a word it no longer resembles. The question is
"what exists here, in time", and it has no text to encode.

**Not a search, and not in the search's module** (RFC-044 section 5.4). No
model, no vector and no score: `SearchHit.similarity` is mandatory on purpose
(RFC-025 section 4.1), and a listing that invented one would present photos
as compared. What it shares with the search is the *request policy* -- the
page size, the radius floor and the two counts of excluded photos -- and it
imports those functions as they are, rather than a copy that could drift.

**Pages by cursor, not by offset** (section 2.1). The cursor is the id of the
last photo of the page before, read back by its primary key: a page can
start where the previous one stopped while a job indexes new photos, which
an offset would shift. One more image than the page is asked for, so
`next_after` is exact -- the map's `MAX_MAP_CELLS + 1` (RFC-032 section 7).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.application.use_cases.search_images import (
    count_hidden_by_unknown_date,
    count_hidden_by_unknown_position,
    validate_limit,
    validate_min_radius,
    validated_request,
)
from app.domain.entities.image import Image
from app.domain.exceptions import ImageNotFoundError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.listing import ListingOrder, ListingSummary
from app.domain.value_objects.search_filters import SearchFilters


@dataclass(frozen=True)
class ListingOverview:
    """The header of a list: its summary and what the filters left out.

    `excluded_unknown_date` and `excluded_unknown_position` are the search's
    numbers, by the search's functions: `None` with no date range, or no
    circle, and then not queried at all (RFC-028 section 8.2). Under a date
    range the undated are out of the list and counted here; without one they
    are *in* the list, at its end, and `listing.undated` says how many
    (RFC-044 section 3.2).
    """

    listing: ListingSummary
    excluded_unknown_date: int | None
    excluded_unknown_position: int | None


@dataclass(frozen=True)
class ImageListing:
    """One page of a listing.

    `next_after` is the id to send as `after` for the next page, and `None`
    exactly when the list is over. `overview` is the first page's, and
    `None` on every page after it: the screen draws the header once, and a
    later page does not pay again for a number it already has.
    """

    order: ListingOrder
    limit: int
    images: list[Image]
    next_after: ImageId | None
    overview: ListingOverview | None


class BrowseImagesUseCase:
    """List the photos the filters allow, by capture date, a page at a time.

    `default_limit` and `min_radius_m` are injected by the composition root,
    from `settings.top_k_results` and `settings.min_radius_m`, as for
    `FindSimilarImagesUseCase` -- the Application layer does not read
    configuration. **No embedding model**: the listing never encodes
    anything, and its provider must not compose one (section 5.4).
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
        filters: SearchFilters,
        order: ListingOrder = ListingOrder.NEWEST,
        limit: int | None = None,
        after: ImageId | None = None,
    ) -> ImageListing:
        """One page of the list, after `after` when there is one.

        1. The page size and the circle are checked first, by the search's
           `validated_request()`: an unusable request costs no read.
        2. `after` is read by its primary key. No row is
           `ImageNotFoundError` -- the last photo of the page before left the
           index -- which the route answers with a 404 and the screen with
           *"A lista mudou desde a última página"*. The photo need not
           satisfy the filters: it is a position in the order.
        3. `limit + 1` images are asked for; the extra one, when it comes,
           only says that the list goes on.
        4. Without `after`, the overview: the repository's summary and the
           search's two counts.

        **Any filters are accepted, none included.** The whole collection by
        date is a valid question that costs the scan of the whole map; the
        interface only asks it with a circle (section 5.1).
        """
        effective_limit = validated_request(
            limit, filters, self._default_limit, self._min_radius_m
        )
        cursor: Image | None = None
        if after is not None:
            cursor = self._repository.get(after)
            if cursor is None:
                raise ImageNotFoundError(
                    f"No indexed image has the id {after}: the list changed since "
                    "the page that ended with it."
                )

        rows = self._repository.list_by_filters(
            filters, order, effective_limit + 1, cursor
        )
        images = rows[:effective_limit]
        next_after = images[-1].id if len(rows) > effective_limit else None

        overview = None
        if after is None:
            overview = ListingOverview(
                listing=self._repository.summarize_listing(filters),
                excluded_unknown_date=count_hidden_by_unknown_date(
                    self._repository, filters
                ),
                excluded_unknown_position=count_hidden_by_unknown_position(
                    self._repository, filters
                ),
            )

        return ImageListing(
            order=order,
            limit=effective_limit,
            images=images,
            next_after=next_after,
            overview=overview,
        )
