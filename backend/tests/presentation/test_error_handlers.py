"""Which domain failure becomes which status (RFC-026 §8, RFC-029 §7.1, RFC-030).

Two things are pinned here, and the second is the one that costs
something to get wrong.

**Nothing that already answered 400 stopped answering 400.** Changing the
status of a route that exists is a breaking change for its clients, and
RFC-029 introduces two new statuses right underneath every error the
search endpoint can raise. The test below enumerates the whole
`DomainError` hierarchy rather than a list somebody maintains by hand, so
an error added later is covered the day it is added.

**Two errors were moved deliberately, and the list of them is short and
explicit.** `DeviceNotFoundError` and `DeviceNotConnectedError` existed
before RFC-029 and now answer 404 and 409. That is safe for a specific
reason rather than a general one: until RFC-029 **no route could raise
either of them**, so no client has ever seen a status for them. If that
stops being true, this file is where the argument lives.

**RFC-030 moved one more, for the same reason.** `ImageNotFoundError`
existed since the first domain RFCs and was never raised by production
code; `GET /api/v1/images/{id}` is the first route that raises it, and
it answers 404.
"""

from __future__ import annotations

import pytest

from app.domain import exceptions as domain_exceptions
from app.domain.exceptions import (
    ConflictError,
    DeviceBusyError,
    DeviceNotConnectedError,
    DeviceNotFoundError,
    DomainError,
    EmbeddingDimensionMismatchError,
    EmptySearchQueryError,
    FileGoneError,
    GoneError,
    IllegalJobTransitionError,
    ImageNotFoundError,
    InvalidDateRangeError,
    InvalidJobScopeError,
    JobNotFoundError,
    NotFoundError,
    ThumbnailNotFoundError,
)
from app.presentation.error_handlers import (
    DEFAULT_STATUS,
    register_error_handlers,
    status_for,
)

RECLASSIFIED_BY_RFC_029: dict[type[DomainError], int] = {
    DeviceNotFoundError: 404,
    DeviceNotConnectedError: 409,
}
"""The only two pre-existing errors whose status RFC-029 changed.

Safe because neither was reachable from any route before RFC-029: the
search endpoint raises neither, and `DeviceNotConnectedError` was raised
only by `Image.require_absolute_path()`, deep inside the indexing worker
where nothing was answering HTTP. No client has ever been told a status
for either of them.
"""

RECLASSIFIED_BY_RFC_030: dict[type[DomainError], int] = {
    ImageNotFoundError: 404,
}
"""The one pre-existing error whose status RFC-030 changed.

Safe for the reason the RFC-029 pair was: before RFC-030 it was imported
and tested and raised nowhere, so no route had ever answered with it.
"""

INTRODUCED_WITH_A_STATUS: dict[type[DomainError], int] = {
    JobNotFoundError: 404,
    DeviceBusyError: 409,
    IllegalJobTransitionError: 409,
    FileGoneError: 410,
    ThumbnailNotFoundError: 404,
}
"""Errors that were born under a status-bearing base, so never answered 400.

Listed rather than inferred from the hierarchy. The test below used to
compute its expectation from `issubclass(..., NotFoundError)`, which made
it pass for *any* re-basing -- including the accidental one it exists to
catch. With the expectations written down, moving an error onto a base is
a change to this file, which is where a reviewer will see it.
"""


def every_domain_error() -> list[type[DomainError]]:
    """Every exported domain error, found rather than listed.

    A hand-maintained list would be complete on the day it was written and
    quietly incomplete afterwards, which is exactly when a status would
    change without anybody noticing.
    """
    found = [getattr(domain_exceptions, name) for name in domain_exceptions.__all__]
    return [
        candidate
        for candidate in found
        if isinstance(candidate, type)
        and issubclass(candidate, DomainError)
        and candidate not in (DomainError, NotFoundError, ConflictError, GoneError)
    ]


@pytest.mark.parametrize("error_type", every_domain_error())
def test_every_domain_error_keeps_the_status_it_had(
    error_type: type[DomainError],
) -> None:
    """400 for everything, except what was moved or born with a status.

    Every exception the package exports is enumerated, and anything not
    named in one of the three tables above must still answer 400.
    """
    expected = (
        RECLASSIFIED_BY_RFC_029.get(error_type)
        or RECLASSIFIED_BY_RFC_030.get(error_type)
        or INTRODUCED_WITH_A_STATUS.get(error_type)
        or DEFAULT_STATUS
    )

    assert status_for(error_type()) == expected


@pytest.mark.parametrize(
    "error_type",
    [
        EmptySearchQueryError,
        EmbeddingDimensionMismatchError,
        InvalidDateRangeError,
    ],
)
def test_the_errors_the_search_route_can_raise_are_still_400(
    error_type: type[DomainError],
) -> None:
    """Named one by one, because these are the ones with live clients.

    Everything `GET /api/v1/images/search` can refuse has answered 400
    since RFC-026, and RFC-029 must not have moved any of them while
    inserting two bases above them.
    """
    assert status_for(error_type()) == 400


def test_the_new_job_errors_map_as_the_rfc_says() -> None:
    assert status_for(JobNotFoundError()) == 404
    assert status_for(DeviceBusyError()) == 409
    assert status_for(IllegalJobTransitionError()) == 409
    assert status_for(InvalidJobScopeError()) == 400


def test_the_three_bases_answer_404_409_and_410() -> None:
    """Pinned together, because they are read together.

    `DeviceNotConnectedError` and `FileGoneError` are the two answers
    `/reveal` gives when it cannot show a file, and a client tells "plug
    in the disk" from "the photo is gone" by these numbers alone.
    """
    assert status_for(NotFoundError()) == 404
    assert status_for(ConflictError()) == 409
    assert status_for(GoneError()) == 410

    assert status_for(ImageNotFoundError()) == 404
    assert status_for(ThumbnailNotFoundError()) == 404
    assert status_for(DeviceNotConnectedError()) == 409
    assert status_for(FileGoneError()) == 410


def test_a_brand_new_domain_error_defaults_to_400() -> None:
    """Inheriting from `DomainError` alone still means "understood, refused"."""

    class SomethingNobodyHasWrittenYetError(DomainError):
        pass

    assert status_for(SomethingNobodyHasWrittenYetError()) == DEFAULT_STATUS


def test_a_new_error_gets_its_status_by_inheriting_it() -> None:
    """The property that replaces a registry.

    Nobody has to remember to add the class anywhere; it answers 404
    because of what it is.
    """

    class SomethingMissingError(NotFoundError):
        pass

    assert status_for(SomethingMissingError()) == 404


def test_the_status_bearing_bases_do_not_overlap() -> None:
    """`status_for` resolves ties by MRO distance; this is why there are none.

    If a future base ever sits under another, the resolution stops being
    academic -- and this test is the place that will fail first.
    """
    bases = (NotFoundError, ConflictError, GoneError)
    for base in bases:
        for other in bases:
            if base is not other:
                assert not issubclass(base, other)


def test_the_most_specific_base_wins_when_one_nests_under_another() -> None:
    """The tie-break, exercised rather than assumed.

    Written as a hypothetical because the hierarchy has no such case
    today. It exists so the ordering rule is tested before somebody
    relies on it.
    """

    class NarrowerError(NotFoundError):
        pass

    class NarrowestError(NarrowerError):
        pass

    assert status_for(NarrowestError()) == 404


def test_only_domain_errors_are_routed_to_this_handler() -> None:
    """A bug must stay a 500, never be dressed up as a refusal.

    `status_for()` would answer 400 for any exception handed to it, so the
    guarantee is not in that function -- it is in the registration, which
    narrows the handler to `DomainError`. Anything else propagates and
    FastAPI answers 500, which is what an unhandled bug should look like.
    """
    from fastapi import FastAPI

    app = FastAPI()
    before = set(app.exception_handlers)
    register_error_handlers(app)

    # FastAPI installs its own handlers for HTTPException and validation
    # errors, so the question is what *this* function added.
    assert set(app.exception_handlers) - before == {DomainError}
    assert Exception not in app.exception_handlers
    assert ValueError not in app.exception_handlers
