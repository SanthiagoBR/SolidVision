"""A half-open range of camera-local capture times (RFC-028 section 8)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.exceptions import InvalidDateRangeError


@dataclass(frozen=True)
class DateRange:
    """`[start, end)`: `start` is included, `end` is not.

    Half-open because "2018" is `[2018-01-01, 2019-01-01)`, which needs no
    knowledge of how many seconds the last day has. The closed form,
    `<= 2018-12-31`, silently drops everything shot after midnight on New
    Year's Eve -- and `<= 2018-12-31T23:59:59` drops whatever lands inside
    the final second. Consecutive half-open ranges also tile without
    overlap, so a photo can never be counted in two adjacent years.

    **Both bounds are naive and must stay naive.** They are compared with
    `images.captured_at`, a `TIMESTAMP WITHOUT TIME ZONE` holding the
    camera's local clock (RFC-028 section 5). An aware bound fails against
    that column in PostgreSQL and raises `TypeError` against a naive value
    in Python: two different failures for one call, exactly what the
    shared repository contract exists to forbid. So it fails here instead,
    identically everywhere, and it is never "helpfully" converted -- which
    zone to convert *to* is the question the column deliberately does not
    answer.

    **Both bounds are required.** An open end is a Presentation concern:
    the HTTP layer turns an omitted `captured_to` into `datetime.max`
    before building this object, so that every consumer here reads two
    datetimes instead of each handling a `None` of its own. The case where
    *neither* bound was given is not an unbounded `DateRange` either; it
    is `SearchFilters.captured_between = None`, which adds no clause at
    all.

    `start == end` is legal and matches nothing. It is the honest meaning
    of the request, and refusing it would make a client that computes
    ranges arithmetically special-case the empty one.
    """

    start: datetime.datetime
    end: datetime.datetime

    def __post_init__(self) -> None:
        for name, bound in (("start", self.start), ("end", self.end)):
            if not isinstance(bound, datetime.datetime):
                raise InvalidDateRangeError(f"Date range {name} must be a datetime.")
            if bound.tzinfo is not None:
                raise InvalidDateRangeError(
                    f"Date range {name} carries a time zone ({bound.isoformat()}); "
                    "capture dates are camera-local times with no zone, so a "
                    "range over them must not have one either."
                )
        if self.start > self.end:
            raise InvalidDateRangeError(
                f"Date range start {self.start.isoformat()} is after its end "
                f"{self.end.isoformat()}."
            )

    def contains(self, moment: datetime.datetime) -> bool:
        """Return whether `moment` falls in `[start, end)`.

        Takes a `datetime`, never `None`. Whether an unknown capture date
        "is in" a range is not a question this object answers: RFC-020's
        rule is that unknown never matches, and every caller has to state
        that explicitly rather than inherit it from a default here.
        """
        return self.start <= moment < self.end
