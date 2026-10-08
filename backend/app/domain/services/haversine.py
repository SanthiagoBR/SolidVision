"""Great-circle distance between two positions, in metres (RFC-032 section 6).

**One implementation**, shared by both in-process `ImageRepository` doubles,
and one constant shared with the PostgreSQL clause that computes the same
formula in SQL. Two hand-written haversines would be two chances to drift
from the database, and the contract test holds all three to one answer --
which only works if the radius of the Earth is the same number everywhere.

Haversine rather than the spherical law of cosines because it stays exact
at short distances: the law of cosines takes `acos` of a value within
rounding error of 1 for two points metres apart, and the photos this filter
exists for are tens of metres apart.

A sphere rather than the WGS 84 ellipsoid. The error is under 0.5% of the
distance, and the uncertainty this filter has to absorb is the offset
between the aircraft and the photographed ground -- tens to hundreds of
metres (RFC-032 section 2.2) -- not the flattening of the planet.
"""

from __future__ import annotations

import math

from app.domain.value_objects.position import Position

EARTH_RADIUS_M = 6_371_008.8
"""The IUGG mean radius of the Earth, in metres.

Imported by `PostgresImageRepository` for its SQL expression, never retyped
there: a contract test whose three implementations disagree by the
difference between 6371 km and 6378 km would fail only for circles drawn
near their edge, which is the hardest failure to read.
"""


def haversine_m(first: Position, second: Position) -> float:
    """Return the great-circle distance between two positions, in metres.

    The longitude difference is taken *before* converting to radians, and
    the PostgreSQL expression does the same. The two orders are equal in
    exact arithmetic and differ in the last bits of a float; matching them
    keeps the in-memory doubles and the database in agreement for a point
    sitting almost exactly on a circle's edge.

    `min(1.0, ...)` guards `asin` against a rounding error pushing its
    argument a hair above 1 for antipodal points.
    """
    first_latitude = math.radians(first.latitude)
    second_latitude = math.radians(second.latitude)
    latitude_delta = math.radians(second.latitude - first.latitude)
    longitude_delta = math.radians(second.longitude - first.longitude)

    h = _squared_sine_of_half(latitude_delta) + math.cos(first_latitude) * math.cos(
        second_latitude
    ) * _squared_sine_of_half(longitude_delta)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _squared_sine_of_half(angle: float) -> float:
    """`sin(angle / 2)` squared, by multiplication -- as the SQL computes it."""
    sine = math.sin(angle / 2.0)
    return sine * sine
