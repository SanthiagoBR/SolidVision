"""Semantic search from the command line, composed over `SearchImagesUseCase`.

There is no non-HTTP entry point for search today (RFC-025/RFC-026 shipped it
only as a FastAPI route). This module composes the same use case the API
uses, the way `job_runner.build_runner()` already composes it for indexing:
`PostgresImageRepository` over a fresh session, and the process-wide CLIP
adapter from `app.presentation.dependencies.get_embedding_model()`.

Two queries: text (`run()`) and a picture (`run_image()`), which ranks the
index against the picture's own CLIP embedding. Either can be narrowed to a
circle -- `--near LAT LON --radius METRES` -- with the API's rules for
`near_lat`/`near_lon`/`radius_m` (RFC-032 section 6), and every hit is
printed with where it was taken.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from app.domain.entities.image import Image
from app.domain.exceptions import (
    EmptySearchQueryError,
    InvalidGeoCircleError,
    InvalidPositionError,
    InvalidSearchLimitError,
)
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position import Position
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHits

# Refusals of the request itself, which the person typing can fix: printed
# as their message, not as a traceback. Anything else -- a database that is
# down, an embedding of the wrong size -- is a fault, and keeps its traceback.
_REQUEST_REFUSALS = (
    EmptySearchQueryError,
    InvalidSearchLimitError,
    InvalidGeoCircleError,
)


def format_position(image: Image) -> str:
    """Where `image` was taken, as `lat, lon` -- or why that is not known.

    Six decimal places (~0.1 m) in `lat, lon` order: the form a map's search
    box accepts pasted as is. It is the aircraft's position, not the ground
    it photographed (RFC-032 section 2.2).

    The two ways of having no position are worded apart because they call
    for different actions. Never examined (`position_source` is `None`) means
    the image was indexed before RFC-032, and `exif_backfill` will read it;
    examined and `unknown` means the file has no usable GPS, and nothing
    will.
    """
    position = image.position
    if position is not None:
        return f"{position.latitude:.6f}, {position.longitude:.6f}"
    if image.position_source is None:
        return "position not read yet"
    return "no position"


def format_hits(hits: SearchHits) -> str:
    """Render ranked hits as one line per result, most similar first.

    A pure function so the output format can be tested without a database
    or a model -- `run()` is the only caller that needs either.
    """
    if not hits:
        return "No results."
    return "\n".join(
        f"{rank}. {hit.similarity:.3f}  {hit.image.relative_path}  "
        f"(device {hit.image.device_id})  {format_position(hit.image)}"
        for rank, hit in enumerate(hits, start=1)
    )


def format_hidden_by_unknown_position(count: int) -> str:
    """Say how many images a circle could not consider for having no position.

    The CLI's `excluded_unknown_position` (RFC-032 section 6.2): without it
    an empty page reads as "no photo was taken here" when the truth may be
    "these photos were never placed anywhere".
    """
    noun = "image has" if count == 1 else "images have"
    return (
        f"{count} indexed {noun} no known position and could not be matched "
        "against the circle."
    )


def search_filters(
    near: Sequence[float] | None, radius_m: float | None
) -> SearchFilters | None:
    """`--near LAT LON` and `--radius METRES` as a filter -- both, or neither.

    `None` when neither was given: no filter at all, which is exactly the
    search this module ran before RFC-032. Half a circle is refused rather
    than completed with a default radius, for the reason the API refuses it
    (RFC-032 section 6): the radius is the part of the question the drone's
    offset makes critical, and only the person asking knows it.

    Called before the model is loaded or the database is opened, so a
    malformed circle costs nothing. The radius floor (`settings.min_radius_m`)
    is not checked here: it is Application policy, and the use case applies
    it with the message that explains it.
    """
    if near is None and radius_m is None:
        return None
    if near is None or radius_m is None:
        raise SystemExit(
            "--near LAT LON and --radius METRES go together: a circle needs "
            "a centre and a radius, and the radius is never assumed."
        )
    latitude, longitude = near
    try:
        circle = GeoCircle(center=Position(latitude, longitude), radius_m=radius_m)
    except (InvalidGeoCircleError, InvalidPositionError) as error:
        raise SystemExit(str(error)) from error
    return SearchFilters(taken_within=circle)


def run(
    query: str,
    limit: int | None,
    near: Sequence[float] | None = None,
    radius_m: float | None = None,
) -> None:
    """Search the index for `query` and print the ranked results."""
    _search(query, limit, search_filters(near, radius_m))


def query_image(path: Path) -> Image:
    """Wrap a file on disk as the transient `Image` an image query needs.

    The embedding adapter reads pixels from an `Image`'s `absolute_path`,
    and an `Image` needs an id and a device. This one is never persisted:
    its device is the nil UUID -- no device -- and its id is derived from
    the path only so that two runs over the same file build the same value.
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


def run_image(
    path: Path,
    limit: int | None,
    near: Sequence[float] | None = None,
    radius_m: float | None = None,
) -> None:
    """Find the indexed images that look most like the picture at `path`.

    The query is the picture itself, encoded by CLIP's image encoder, so a
    photo of a printed photo -- or any copy -- can find its original
    without anyone describing it in words. Crop away whatever is not the
    picture before querying (a table, a hand, a frame): CLIP sees the whole
    file, and the background is part of what it compares.

    A missing file is refused before the model is loaded or the database
    is opened, so the error names the path rather than a decoder.
    """
    if not path.is_file():
        raise SystemExit(f"No image file at {path}")
    _search(query_image(path), limit, search_filters(near, radius_m))


def _search(
    query: str | Image, limit: int | None, filters: SearchFilters | None
) -> None:
    """Compose the use case the way the API does, run one query, print the page.

    Function-local imports for the same reason `indexing_worker.run()` uses
    them: importing this module must not drag SQLAlchemy or the CLIP
    adapter into an import graph that a test of `app.cli.__main__`'s
    argument parsing does not need to pay for.

    `min_radius_m` is injected from settings as
    `get_search_images_use_case()` injects it, so a circle the API would
    refuse is refused here too. The count of images the circle could not
    consider is printed only when it is not zero; without a circle the use
    case does not even ask the repository for it.
    """
    from app.application.use_cases.search_images import SearchImagesUseCase
    from app.infrastructure.config.settings import settings
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    session = SessionLocal()
    try:
        use_case = SearchImagesUseCase(
            repository=PostgresImageRepository(session),
            embedding_model=get_embedding_model(),
            default_limit=settings.top_k_results,
            min_radius_m=settings.min_radius_m,
        )
        if isinstance(query, Image):
            hits = use_case.execute_similar_to(query, limit=limit, filters=filters)
        else:
            hits = use_case.execute(query, limit=limit, filters=filters)
        hidden = use_case.count_hidden_by_unknown_position(filters)
    except _REQUEST_REFUSALS as error:
        raise SystemExit(str(error)) from error
    finally:
        session.close()

    print(format_hits(hits))
    if hidden:
        print(format_hidden_by_unknown_position(hidden))
