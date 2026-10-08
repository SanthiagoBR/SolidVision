"""Position domain exceptions (RFC-032)."""

from app.domain.exceptions.domain_error import DomainError


class InvalidPositionError(DomainError):
    """Raised when a latitude and longitude do not describe one point on Earth.

    Most often: half a position. A latitude without its longitude does not
    locate anything, and RFC-032 section 4.3 refuses to store one -- the
    `ck_images_position_pairing` CHECK says the same thing in SQL, for the
    writers that never pass through here. Also raised for a coordinate
    outside `[-90, 90]` x `[-180, 180]`, or one that is not a finite number.

    A subclass of `DomainError`, so the HTTP layer answers 400 without a
    handler of its own: `near_lat=91` parses as a number and is refused for
    what it means, which is the 400 case rather than FastAPI's 422.
    """

    def __init__(self, message: str = "Invalid position.") -> None:
        super().__init__(message)


class InvalidGeoCircleError(DomainError):
    """Raised when a "near here" circle cannot be evaluated as asked.

    A radius that is zero, negative, not finite, or -- by application
    policy -- below `MIN_RADIUS_M`; or a circle given only in part, two of
    `near_lat` / `near_lon` / `radius_m` without the third (RFC-032
    section 6). Refused rather than answered with an empty result: an
    empty answer to a malformed circle reads exactly like "there is no
    photo here", which is the most expensive wrong answer this filter can
    give.
    """

    def __init__(self, message: str = "Invalid search circle.") -> None:
        super().__init__(message)


class InvalidBoundingBoxError(DomainError):
    """Raised when a map area is inverted or crosses the antimeridian.

    `GET /images/map` takes the viewport as `min`/`max` corners, and
    `min > max` has two readings that the four numbers cannot tell apart:
    a client that swapped its corners, or a viewport that wraps across
    +/-180 degrees. RFC-032 section 7 supports neither and says so with a
    400, rather than guessing which one was meant and answering a
    different question.
    """

    def __init__(self, message: str = "Invalid map area.") -> None:
        super().__init__(message)
