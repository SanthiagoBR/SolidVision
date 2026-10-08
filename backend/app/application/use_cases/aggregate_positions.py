"""Where the photos are, as cells a map can draw (RFC-032 section 7).

A map cannot be drawn from what search returns. Search hands back at most
`MAX_SEARCH_LIMIT` ranked hits, so a map built from them would show where the
hundred photos *most like the query* are -- which does not answer "where are
my photos". And fetching 100,000 points for the client to cluster moves the
collection across the wire to answer a question `GROUP BY` answers in the
database.

**This use case ignores the query text, and that is the decision that has to
be written down**, because the alternative looks better and is not: a map that
reflected the query could only aggregate the top K, and the top 100 of a
100,000-image index describes nothing about geography. The map shows where the
photos *the filters allow* are; ranking stays the search's job. Two questions,
two endpoints.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.position import BoundingBox, PositionCell
from app.domain.value_objects.search_filters import SearchFilters

DEFAULT_MAP_PRECISION = 3
"""Decimal places of a degree when the client does not ask: ~100 m cells.

The cell size RFC-032 section 2.3 measured the pilot with -- 40 photos in 24
cells -- and the order of magnitude of the aircraft offset the search radius
has to absorb, so a cell is about as precise as a position is worth.
"""

MAX_MAP_PRECISION = 6
"""The finest cell offered: ~0.1 m, already below what a drone's GNSS knows.

A request for more is served at this precision and says so through
`precision_applied`, the way an over-fine request is answered everywhere else
in this use case -- the response echoes what was applied, rather than
refusing a number that could only ever make the cells meaningless.
"""


@dataclass(frozen=True)
class PositionMap:
    """One map answer: the cells, the precision they were cut at, and what is missing.

    `precision_applied` echoes the precision actually used, as `limit` does
    for search (RFC-026): without it, a client that asked for 100 m cells and
    received 10 km ones could not tell a sparse collection from a reduced
    answer.

    `excluded_unknown_position` is the number of photos under the same
    filters that have no coordinates and so cannot be on any map -- the
    number of RFC-032 section 6.2, in the place the map needs it.
    """

    precision_requested: int
    precision_applied: int
    cells: list[PositionCell]
    excluded_unknown_position: int


class AggregatePositionsUseCase:
    """Group the photos in a viewport into at most `max_cells` map cells.

    `max_cells` is injected from `settings.max_map_cells` by the composition
    root, never read from settings here, for the reason `MAX_SEARCH_LIMIT`'s
    neighbour `default_limit` is: the Application layer does not import
    configuration, and `test_application_architecture.py` enforces it.
    """

    def __init__(self, repository: ImageRepository, max_cells: int) -> None:
        if max_cells < 1:
            raise ValueError(f"max_cells must be at least 1, got {max_cells}")
        self._repository = repository
        self._max_cells = max_cells

    def execute(
        self,
        area: BoundingBox,
        filters: SearchFilters | None = None,
        precision: int | None = None,
    ) -> PositionMap:
        """Return the cells of `area` under `filters`, coarsening until they fit.

        **The universe is the filters'.** Images with an embedding -- the
        ones a search could return -- on the requested devices, in the
        requested date range, with a position inside `area`. A circle in
        `filters` is dropped rather than applied: the viewport is the map's
        area, and "near here" is the search's question (RFC-032 section 9).

        **Above `max_cells`, precision drops one decimal place at a time**
        until the cells fit, and the response says which precision it
        landed on. Coarser cells hide no photo -- every one is still
        counted, in a bigger square -- where truncating the list would hide
        whole places. Each step is one grouped query; most viewports fit on
        the first.

        At precision 0 (1-degree cells) there is nothing coarser to fall back
        to, and every cell is returned even if that exceeds `max_cells`: at
        most 181 x 361 by geometry, and for a collection that fits in one
        country, a handful. Exceeding the ceiling there is the honest
        failure; dropping photos to honour it would not be.
        """
        requested = DEFAULT_MAP_PRECISION if precision is None else precision
        if requested < 0:
            raise ValueError(f"precision must not be negative, got {requested}")
        universe = dataclasses.replace(filters or SearchFilters(), taken_within=None)

        applied = min(requested, MAX_MAP_PRECISION)
        while True:
            limit = self._max_cells + 1 if applied > 0 else None
            cells = self._repository.aggregate_positions(universe, area, applied, limit)
            if len(cells) <= self._max_cells or applied == 0:
                break
            applied -= 1

        return PositionMap(
            precision_requested=requested,
            precision_applied=applied,
            cells=cells,
            excluded_unknown_position=self._repository.count_unknown_position(universe),
        )
