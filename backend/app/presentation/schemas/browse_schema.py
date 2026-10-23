"""Response shape of `GET /api/v1/images/browse` (RFC-044 sections 5.1 and 5.3).

The photos are `ImageSchema` -- the body of `GET /images/{id}`, field for field,
by the same `image_fields()` -- and **nothing more**: no `similarity`, because a
listing compared nothing (RFC-025 section 4.1). The rest is how to ask for the
next page, and, on the first page only, the list at a glance.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, Field

from app.application.use_cases.browse_images import ImageListing, ListingOverview
from app.application.use_cases.resolve_image_location import LocatedImage
from app.domain.value_objects.listing import ListingOrder
from app.presentation.schemas.image_schema import ImageSchema


class ListingSummarySchema(BaseModel):
    """The whole list under the filters, not this page (RFC-044 section 5.3)."""

    total: int = Field(
        description="How many photos the whole list has, the undated included"
    )
    undated: int = Field(
        description=(
            "How many of them have no capture date: they come last, in folder "
            "order, in both directions. 0 under a date range, which an unknown "
            "date never matches."
        )
    )
    oldest_captured_at: datetime.datetime | None = Field(
        description=(
            "The earliest capture date of the list, camera-local with no time "
            "zone, as captured_at. Null when no photo of the list has a date."
        )
    )
    newest_captured_at: datetime.datetime | None = Field(
        description="The latest capture date of the list; null as oldest_captured_at"
    )
    excluded_unknown_date: int | None = Field(
        description=(
            "How many indexed photos the capture-date range left out because "
            "their date is unknown, as the search reports it. Null when no date "
            "range was sent: without one, those photos are in the list."
        )
    )
    excluded_unknown_position: int | None = Field(
        description=(
            "How many indexed photos the circle left out because they have no "
            "position, as the search reports it. Null when no circle was sent."
        )
    )

    @classmethod
    def from_overview(cls, overview: ListingOverview) -> ListingSummarySchema:
        listing = overview.listing
        return cls(
            total=listing.total,
            undated=listing.undated,
            oldest_captured_at=listing.oldest,
            newest_captured_at=listing.newest,
            excluded_unknown_date=overview.excluded_unknown_date,
            excluded_unknown_position=overview.excluded_unknown_position,
        )


class BrowseResponseSchema(BaseModel):
    """One page of every photo under the filters, by capture date.

    `limit` echoes the page size applied, as the searches do (RFC-026).
    `summary` is the first page's and `null` on the pages after it, so that
    inside it `null` keeps meaning "there was no such filter" and never
    "this page did not count".
    """

    order: ListingOrder = Field(description="The direction of the capture date")
    limit: int = Field(description="The page size actually applied")
    results: list[ImageSchema] = Field(
        description=(
            "The photos of this page: by capture date in the direction of "
            "order, the undated last in both; then by relative_path and id, "
            "ascending"
        )
    )
    next_after: UUID | None = Field(
        description=(
            "The id to send as after for the next page; null when the list is "
            "over. Exact: never null while photos remain."
        )
    )
    summary: ListingSummarySchema | None = Field(
        description="The whole list at a glance, on the first page; null after it"
    )

    @classmethod
    def from_listing(
        cls, listing: ImageListing, locations: Sequence[LocatedImage]
    ) -> BrowseResponseSchema:
        """Shape one page for the wire, in the repository's order.

        `locations` is `ResolveImageLocationUseCase`'s answer for the page's
        images, in the same order -- it keeps order by contract -- and the
        pairing is checked, so a mismatch is an error rather than one photo
        published under another's path.
        """
        results = []
        for image, located in zip(listing.images, locations, strict=True):
            if located.image.id != image.id:
                raise ValueError(
                    f"Location for {located.image.id} paired with {image.id}"
                )
            results.append(ImageSchema.from_located(located))
        return cls(
            order=listing.order,
            limit=listing.limit,
            results=results,
            next_after=listing.next_after.value if listing.next_after else None,
            summary=(
                ListingSummarySchema.from_overview(listing.overview)
                if listing.overview is not None
                else None
            ),
        )
