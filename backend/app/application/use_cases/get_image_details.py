"""One indexed image, where it is, and whether its disk is here (RFC-030 section 4.2).

`GET /api/v1/images/{id}` -- the route RFC-026's `search_schema.py` promised
to "RFC-027", which became devices, and which RFC-030 delivers three
numbers later.
"""

from __future__ import annotations

from app.application.use_cases.resolve_image_location import (
    LocatedImage,
    ResolveImageLocationUseCase,
)
from app.domain.exceptions import ImageNotFoundError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.image_id import ImageId


class GetImageDetailsUseCase:
    """Return one image with its device and, when mounted, its absolute path."""

    def __init__(
        self,
        repository: ImageRepository,
        location_resolver: ResolveImageLocationUseCase,
    ) -> None:
        self._repository = repository
        self._locations = location_resolver

    def execute(self, image_id: ImageId) -> LocatedImage:
        """Return the image, raising only when the id names no row.

        **A disconnected device is a successful answer.** The row exists,
        and the row is what this describes; `connected: false` with the
        device's label is the "it is on HD3" result RFC-030 section 4.1
        exists to give, and a 404 or a 409 here would turn it into an error.

        No similarity, because there is no query: a score means nothing
        outside the search that produced it (RFC-025 section 4.1).
        """
        image = self._repository.get(image_id)
        if image is None:
            raise ImageNotFoundError(f"No indexed image with id {image_id.value}.")
        return self._locations.execute_one(image)
