"""Where on Earth a photograph was taken, and what the file said about it (RFC-032).

Three values, kept apart because they answer different questions:

    Position         a point: latitude and longitude, ranges validated
    PositionReading  the outcome of examining one file -- a point and its
                     source, or `unknown`
    BoundingBox      an axis-aligned area: the map's viewport, and the
                     pre-filter a search circle hands the database
    PositionCell     one square of the map, and how many photos are in it

**The vocabulary is `position`, never `location`** (RFC-032 section 4.5).
In this codebase *location* already means where the *file* is -- a mount
point and an absolute path, resolved per request by
`ResolveImageLocationUseCase` -- and a reviewer reading the word must never
have to ask which of the two is meant.

**Unit-naive on purpose.** A coordinate here is decimal degrees as the GNSS
receiver recorded them, WGS 84, with no datum conversion and no unit of its
own beyond "degrees". Metres appear in exactly one place, a circle's radius
(`GeoCircle`), and nowhere is one converted into the other except by the
haversine distance that compares them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain.exceptions import InvalidBoundingBoxError, InvalidPositionError
from app.domain.value_objects.position_source import PositionSource

MAX_LATITUDE = 90.0
MAX_LONGITUDE = 180.0


@dataclass(frozen=True)
class Position:
    """A point on Earth, in decimal degrees.

    The range checks are the same ones `ck_images_latitude_range` and
    `ck_images_longitude_range` enforce in SQL (RFC-032 section 5). Both
    exist on purpose: this one produces a message a person can read, and
    the CHECK is the part no future writer -- a manual `UPDATE`, a careless
    backfill -- can go around.

    **`0, 0` is a valid `Position`.** It is a real point in the Gulf of
    Guinea, and a map click there is a legitimate search centre. That the
    *EXIF* `0, 0` is a placeholder for "no satellite fix" is a fact about
    how receivers write files, and it is recognised by name where files are
    read (`exif_capture_date.py`), not hidden inside a range check here.
    """

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        _require_coordinate("latitude", self.latitude, MAX_LATITUDE)
        _require_coordinate("longitude", self.longitude, MAX_LONGITUDE)


@dataclass(frozen=True)
class PositionReading:
    """The outcome of examining one file for where it was taken.

    The `CaptureDate` of position: always the result of an examination.
    "Never examined" is not a `PositionReading` at all -- it is the
    *absence* of one, `None` wherever a `PositionReading | None` is
    expected -- and that asymmetry is the NULL / `'unknown'` distinction of
    RFC-032 section 4.4 expressed as a type.

    Distinct from `Position` because the two are used differently: a
    `GeoCircle` is centred on a point, which has no source; a file yields a
    reading, which may have no point.
    """

    position: Position | None
    source: PositionSource

    def __post_init__(self) -> None:
        if self.position is not None and not isinstance(self.position, Position):
            raise InvalidPositionError("A position reading needs a Position or None.")
        if not isinstance(self.source, PositionSource):
            raise InvalidPositionError("Position source must be a PositionSource.")
        if (self.position is None) != (self.source is PositionSource.UNKNOWN):
            raise InvalidPositionError(
                f"Position source {self.source.value!r} does not match "
                f"{'a missing' if self.position is None else 'a present'} "
                "position: 'unknown' is exactly the case with no position."
            )

    @classmethod
    def unknown(cls) -> PositionReading:
        """The file was examined and holds no usable position."""
        return cls(position=None, source=PositionSource.UNKNOWN)

    @property
    def latitude(self) -> float | None:
        return self.position.latitude if self.position is not None else None

    @property
    def longitude(self) -> float | None:
        return self.position.longitude if self.position is not None else None


@dataclass(frozen=True)
class BoundingBox:
    """An axis-aligned area, corners included: `min <= value <= max` on both axes.

    Two users, one shape. `GET /images/map` receives the viewport as one,
    and `GeoCircle.bounding_box()` produces one as the pre-filter that lets
    an index help a circle search.

    **`min > max` is refused on either axis.** On latitude it can only mean
    the corners were swapped. On longitude it means the same -- or a
    viewport that wraps across +/-180 degrees, which RFC-032 section 7
    declares unsupported for now. The four numbers cannot tell those two
    apart, so neither is guessed at.

    A box of zero width or height is legal and matches only what lies
    exactly on it, the way `DateRange(x, x)` is legal and matches nothing.
    """

    min_latitude: float
    min_longitude: float
    max_latitude: float
    max_longitude: float

    def __post_init__(self) -> None:
        Position(self.min_latitude, self.min_longitude)
        Position(self.max_latitude, self.max_longitude)
        if self.min_latitude > self.max_latitude:
            raise InvalidBoundingBoxError(
                f"Map area min_lat {self.min_latitude} is north of max_lat "
                f"{self.max_latitude}."
            )
        if self.min_longitude > self.max_longitude:
            raise InvalidBoundingBoxError(
                f"Map area min_lon {self.min_longitude} is east of max_lon "
                f"{self.max_longitude}. Either the corners are swapped or the "
                "area crosses the 180th meridian, which is not supported."
            )

    def contains(self, position: Position) -> bool:
        """Whether `position` lies in the box, edges included."""
        return (
            self.min_latitude <= position.latitude <= self.max_latitude
            and self.min_longitude <= position.longitude <= self.max_longitude
        )


@dataclass(frozen=True)
class PositionCell:
    """One square of the map: its centre, and how many photos fall in it (RFC-032 §7).

    The centre is the coordinate pair rounded to the requested number of
    decimal places, which is the middle of the square of everything that
    rounds to it -- 3 places is roughly 100 m. A cell exists only because
    at least one photo is in it, so `count` is never zero.

    Lives in Domain beside `Position` because it is part of the
    `ImageRepository` port's contract, as `SearchHit` is.
    """

    latitude: float
    longitude: float
    count: int


# Every `ImageRepository` implementation defines a `list()` method, which
# shadows the builtin inside its class body; `-> list[PositionCell]` written
# on one of its methods would resolve to the method. Evaluated here, at
# module scope, for the reason `SearchHits` is.
PositionCells = list[PositionCell]


def validate_position_fields(
    latitude: float | None,
    longitude: float | None,
    source: PositionSource | None,
) -> None:
    """Enforce the pairing rules for a position held as three loose fields.

    Used by `Image`, which stores the halves separately because they are
    three columns. The rule with teeth is the first one -- **never half a
    position** (RFC-032 section 4.3): a latitude with no longitude would pass
    every range check and locate nothing. `ck_images_position_pairing` says
    the same in SQL.

    `source is None` is legal here and nowhere in `PositionReading`: on
    `Image` it means the row was never examined, and then there can be no
    coordinates either.
    """
    if (latitude is None) != (longitude is None):
        raise InvalidPositionError(
            "A position needs both a latitude and a longitude; got only the "
            f"{'longitude' if latitude is None else 'latitude'}."
        )
    position = (
        Position(latitude, longitude)
        if latitude is not None and longitude is not None
        else None
    )
    if source is None:
        if position is not None:
            raise InvalidPositionError("A position needs the source it was read from.")
        return
    PositionReading(position=position, source=source)


def _require_coordinate(name: str, value: object, limit: float) -> None:
    """A finite number within `[-limit, limit]`, or `InvalidPositionError`.

    `bool` is refused although it is an `int` subclass: `True` as a latitude
    is a caller bug, not the equator plus one degree.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidPositionError(f"Position {name} must be a number.")
    if not math.isfinite(value):
        raise InvalidPositionError(f"Position {name} must be finite, got {value}.")
    if not -limit <= value <= limit:
        raise InvalidPositionError(
            f"Position {name} {value} is outside [-{limit:g}, {limit:g}]."
        )
