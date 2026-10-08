"""A point and a radius: "photos taken near here" (RFC-032 section 6).

**The query is a circle, never a point.** The user does not know coordinates;
they know a place when they see it on a map, so the search is a click and a
distance -- and the distance is part of the question, not an implementation
detail (RFC-032 section 2.2). The coordinate in the file is where the
*aircraft* was, tens to hundreds of metres from what it photographed, and the
radius is what absorbs that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain.exceptions import InvalidGeoCircleError
from app.domain.services.haversine import EARTH_RADIUS_M, haversine_m
from app.domain.value_objects.position import (
    MAX_LATITUDE,
    MAX_LONGITUDE,
    BoundingBox,
    Position,
)

_BOX_MARGIN = 1e-9
"""Relative slack on the pre-filter box's radius, so it can only ever be too large.

The box must contain the whole circle or it hides photos the exact distance
would have kept. In exact arithmetic it does; in floating point a point on
the circle's very edge could land a rounding error outside it. A billionth
of the radius -- a micrometre on a kilometre -- removes that without letting
in anything the exact clause then has to discard in noticeable numbers.
"""

_BOX_PAD_DEGREES = 1e-9
"""Absolute slack on every side of the box: about 0.1 mm.

The relative margin vanishes for a small radius, and the rounding it guards
against is in the *degrees*, whose last bit near 90 is ~1e-14. Belt and
braces, and free: a tenth of a millimetre lets in nothing.
"""

_MAX_HALF_WIDTH_SINE = 1 - 1e-6
"""Above this, `asin` is too steep to trust, and the box is dropped instead.

The longitude half-width is `asin(sin(d) / cos(latitude))`, and `asin` has
an infinite slope at 1: for a circle that almost reaches a pole, a rounding
error in the argument becomes a large error in the width. Those circles are
inside a hair's breadth of the pole case, which has no box anyway.
"""


@dataclass(frozen=True)
class GeoCircle:
    """Every position within `radius_m` metres of `center`, edge included.

    **`radius_m` must be positive and finite, and a zero radius is refused**
    -- where `DateRange(x, x)` is accepted as an empty interval. The
    difference is deliberate (RFC-032 section 6): "from X until X" is what a
    client computing ranges arithmetically legitimately produces, while no
    click on a map produces a circle of radius zero. Refusing says so;
    answering with nothing would look exactly like "no photo here".

    **No upper bound.** "Anywhere in Brazil" is a legitimate request; what a
    large radius changes is selectivity, not correctness. A radius larger
    than half the planet simply contains every position.

    **Metres, everywhere.** In this value object, in the SQL, and in the
    HTTP parameter: two units and a conversion between them is a bug no
    test notices until it inverts (RFC-032 section 9).

    The application's minimum (`MIN_RADIUS_M`) is not checked here. It is
    product policy about aircraft offsets, not a property of circles, and
    it lives in the Application layer where it is injected from settings.
    """

    center: Position
    radius_m: float

    def __post_init__(self) -> None:
        if not isinstance(self.center, Position):
            raise InvalidGeoCircleError("A search circle needs a Position centre.")
        radius = self.radius_m
        if isinstance(radius, bool) or not isinstance(radius, int | float):
            raise InvalidGeoCircleError("A search radius must be a number of metres.")
        if not math.isfinite(radius) or radius <= 0:
            raise InvalidGeoCircleError(
                f"A search radius must be a positive number of metres, got {radius}."
            )

    def contains(self, position: Position) -> bool:
        """Whether `position` is within the radius, by exact haversine distance.

        Takes a `Position`, never `None`. Whether an unknown position "is
        near" anything is not a question this object answers: RFC-020's rule
        is that unknown never matches, and every caller has to say so
        explicitly rather than inherit it from a default here.
        """
        return haversine_m(self.center, position) <= self.radius_m

    def bounding_box(self) -> BoundingBox | None:
        """The pre-filter box that contains the circle, or `None` when there is none.

        **Disposable by correctness.** The box exists only so an index can
        narrow the rows before the exact distance runs, and the distance is
        what answers. Two circles have no correct axis-aligned box in
        `min <= x <= max` form, and for them this returns `None` so that only
        the exact clause is emitted -- slower, and right:

        - **a circle containing a pole**: every longitude is inside it, and
          the longitude half-width, `asin(sin(d) / cos(latitude))`, has no
          value once `d` reaches the pole;
        - **a circle crossing the 180th meridian**: its box would wrap, which
          `min <= longitude <= max` cannot express -- an inverted box would
          match nothing.

        For a collection in Brazil neither ever happens, which is exactly why
        it has to be written down: the case that never fires is the one
        nobody tests.

        The longitude half-width uses the exact spherical-cap formula rather
        than `d / cos(latitude)`, which is an approximation that undershoots
        -- and a box that undershoots hides photos.
        """
        angular = self.radius_m / EARTH_RADIUS_M * (1 + _BOX_MARGIN)
        latitude = math.radians(self.center.latitude)
        south = latitude - angular
        north = latitude + angular
        if north >= math.pi / 2 or south <= -math.pi / 2:
            return None

        half_width_sine = math.sin(angular) / math.cos(latitude)
        if half_width_sine > _MAX_HALF_WIDTH_SINE:
            return None
        half_width = math.degrees(math.asin(half_width_sine)) + _BOX_PAD_DEGREES
        west = self.center.longitude - half_width
        east = self.center.longitude + half_width
        if west < -MAX_LONGITUDE or east > MAX_LONGITUDE:
            return None

        return BoundingBox(
            min_latitude=max(-MAX_LATITUDE, math.degrees(south) - _BOX_PAD_DEGREES),
            min_longitude=west,
            max_latitude=min(MAX_LATITUDE, math.degrees(north) + _BOX_PAD_DEGREES),
            max_longitude=east,
        )
