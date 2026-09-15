"""`SearchFilters` and the one question every repository asks of it."""

from __future__ import annotations

import datetime
import uuid

from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.search_filters import SearchFilters

YEAR_2018 = DateRange(datetime.datetime(2018, 1, 1), datetime.datetime(2019, 1, 1))


def test_a_default_filter_is_empty() -> None:
    filters = SearchFilters()

    assert filters.is_empty()
    assert filters.captured_between is None


def test_a_device_filter_is_not_empty() -> None:
    assert not SearchFilters(device_ids=frozenset({DeviceId(uuid.uuid4())})).is_empty()


def test_a_date_filter_alone_is_not_empty() -> None:
    """The regression `is_empty()` was named to prevent.

    RFC-027 wrote it as `not self.device_ids`. Had RFC-028 added its field
    without updating it, a date-only filter would read as empty, every
    repository would skip its clauses, and the range would be silently
    ignored -- a search for 2018 returning every year.
    """
    assert not SearchFilters(captured_between=YEAR_2018).is_empty()


def test_both_fields_can_be_set_together() -> None:
    device_id = DeviceId(uuid.uuid4())

    filters = SearchFilters(
        device_ids=frozenset({device_id}), captured_between=YEAR_2018
    )

    assert filters.device_ids == frozenset({device_id})
    assert filters.captured_between == YEAR_2018
    assert not filters.is_empty()


def test_filters_stay_hashable() -> None:
    assert hash(SearchFilters(captured_between=YEAR_2018)) == hash(
        SearchFilters(captured_between=YEAR_2018)
    )
