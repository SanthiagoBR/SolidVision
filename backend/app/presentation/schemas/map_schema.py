"""Response shape for `GET /api/v1/images/map` (RFC-032 section 7).

The contract a map draws from, defined before the map exists: `frontend/` is
empty and there is no UI RFC yet, so this is the shape that RFC will consume.
Cells rather than photos -- a centre and a count per square -- because the
question is "where are my photos", and the answer to that for 100,000 photos
is a few hundred squares, not 100,000 points.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.application.use_cases.aggregate_positions import PositionMap


class MapCellSchema(BaseModel):
    """One square of the grid: where its centre is, and how many photos are in it."""

    latitude: float = Field(
        description=(
            "Centre of the cell, decimal degrees: the photos' latitude rounded "
            "to precision_applied places"
        )
    )
    longitude: float = Field(
        description=(
            "Centre of the cell, decimal degrees: the photos' longitude rounded "
            "to precision_applied places"
        )
    )
    count: int = Field(description="How many photos fall in the cell; never 0")


class MapResponseSchema(BaseModel):
    """The body of a map request.

    `precision_applied` echoes the precision actually used, the way search
    echoes `limit` (RFC-026): when the requested grid would have had more
    than `MAX_MAP_CELLS` squares, the grid was coarsened, and a client that
    could not see that would mistake bigger squares for a sparser
    collection.
    """

    precision_applied: int = Field(
        description=(
            "Decimal places of a degree the cells were rounded to (3 is about "
            "100 m). Lower than requested when the requested grid had too many "
            "cells, or than the finest grid offered."
        )
    )
    cells: list[MapCellSchema] = Field(
        description=(
            "Every non-empty cell in the area, under the device and date "
            "filters, ordered by latitude then longitude. Only images with an "
            "embedding -- the ones a search could return -- are counted."
        )
    )
    excluded_unknown_position: int = Field(
        description=(
            "How many photos under the same device and date filters have no "
            "position, and so are on no map at all -- the same number a 'near "
            "here' search reports. Not restricted to the area: a photo with no "
            "coordinates is in no area."
        )
    )

    @classmethod
    def from_map(cls, answer: PositionMap) -> MapResponseSchema:
        """Shape the use case's answer for the wire, adding nothing."""
        return cls(
            precision_applied=answer.precision_applied,
            cells=[
                MapCellSchema(
                    latitude=cell.latitude, longitude=cell.longitude, count=cell.count
                )
                for cell in answer.cells
            ],
            excluded_unknown_position=answer.excluded_unknown_position,
        )
