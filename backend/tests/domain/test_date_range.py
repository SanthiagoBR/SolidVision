"""`DateRange`, the half-open capture-date window of RFC-028 section 8."""

from __future__ import annotations

import datetime
from dataclasses import FrozenInstanceError

import pytest

from app.domain.exceptions import DomainError, InvalidDateRangeError
from app.domain.value_objects.date_range import DateRange

NEW_YEAR_2018 = datetime.datetime(2018, 1, 1)
NEW_YEAR_2019 = datetime.datetime(2019, 1, 1)
YEAR_2018 = DateRange(start=NEW_YEAR_2018, end=NEW_YEAR_2019)


def test_the_start_is_included() -> None:
    assert YEAR_2018.contains(NEW_YEAR_2018)


def test_the_end_is_excluded() -> None:
    """The half of "half-open" that makes consecutive years tile.

    If the end were included, a photo taken at the stroke of midnight
    would belong to both 2018 and 2019.
    """
    assert not YEAR_2018.contains(NEW_YEAR_2019)


def test_the_last_moment_before_the_end_is_included() -> None:
    """The bug a closed `<= 2018-12-31` range has, and this one does not."""
    new_years_eve_shot = datetime.datetime(2018, 12, 31, 23, 59, 59, 999_999)

    assert YEAR_2018.contains(new_years_eve_shot)


def test_a_moment_before_the_start_is_excluded() -> None:
    assert not YEAR_2018.contains(datetime.datetime(2017, 12, 31, 23, 59, 59))


def test_start_equal_to_end_is_a_legal_empty_range() -> None:
    """Refusing it would make arithmetic clients special-case zero width."""
    empty = DateRange(start=NEW_YEAR_2018, end=NEW_YEAR_2018)

    assert not empty.contains(NEW_YEAR_2018)


def test_an_inverted_range_is_rejected() -> None:
    with pytest.raises(InvalidDateRangeError, match="after its end"):
        DateRange(start=NEW_YEAR_2019, end=NEW_YEAR_2018)


@pytest.mark.parametrize("bound", ["start", "end"])
def test_a_zone_aware_bound_is_rejected(bound: str) -> None:
    """Never converted: which zone to convert to is the unanswerable question.

    Against the `TIMESTAMP WITHOUT TIME ZONE` column an aware value fails
    in PostgreSQL, and against a naive `datetime` in Python it raises
    `TypeError` -- two different failures for one call. Refusing it here
    makes every implementation fail the same way.
    """
    aware = datetime.datetime(2018, 6, 1, tzinfo=datetime.UTC)
    bounds = {"start": NEW_YEAR_2018, "end": NEW_YEAR_2019, bound: aware}

    with pytest.raises(InvalidDateRangeError, match="time zone"):
        DateRange(**bounds)


def test_a_zero_offset_zone_is_still_a_zone() -> None:
    """UTC+00:00 is not "naive with extra steps"; it is a claim about zones."""
    zero_offset = datetime.timezone(datetime.timedelta(0))

    with pytest.raises(InvalidDateRangeError):
        DateRange(start=NEW_YEAR_2018.replace(tzinfo=zero_offset), end=NEW_YEAR_2019)


def test_a_date_that_is_not_a_datetime_is_rejected() -> None:
    """`date` compares with `datetime` inconsistently enough to be a trap."""
    with pytest.raises(InvalidDateRangeError):
        DateRange(start=datetime.date(2018, 1, 1), end=NEW_YEAR_2019)  # type: ignore[arg-type]


def test_the_open_ended_extremes_are_expressible() -> None:
    """Presentation fills an omitted bound with these, never with `None`."""
    unbounded = DateRange(start=datetime.datetime.min, end=datetime.datetime.max)

    assert unbounded.contains(datetime.datetime(1826, 1, 1))
    assert unbounded.contains(datetime.datetime(2099, 1, 1))


def test_the_error_is_a_domain_error() -> None:
    """So the HTTP layer answers 400 through the one existing handler."""
    assert issubclass(InvalidDateRangeError, DomainError)


def test_date_range_is_immutable_and_hashable() -> None:
    with pytest.raises(FrozenInstanceError):
        YEAR_2018.start = NEW_YEAR_2019  # type: ignore[misc]

    assert hash(YEAR_2018) == hash(DateRange(NEW_YEAR_2018, NEW_YEAR_2019))
