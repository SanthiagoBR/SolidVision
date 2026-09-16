"""Scope validation and overlap normalisation (RFC-029 sections 5.2 and 7.1).

Two separate jobs in one module, and they fail in opposite directions.
Validation is about *escaping the device*: everything it rejects would
have indexed somewhere the job never named. Normalisation is about
*visiting a file twice*, which breaks the checkpoint -- a position is only
meaningful in a sequence that visits each file once.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest

from app.domain.exceptions import InvalidJobScopeError
from app.domain.value_objects.job_scope import JobScope, normalize_scopes

BACKSLASH = chr(92)


def test_a_relative_path_becomes_its_parts() -> None:
    assert JobScope("2018/junho").parts == ("2018", "junho")
    assert str(JobScope("2018/junho")) == "2018/junho"


def test_windows_separators_are_normalised_rather_than_refused() -> None:
    """A Windows client sends `2018\\junho`, and it means the same folder.

    Reading it as one part named `2018\\junho` would be worse than
    refusing it: it would pass every check here and then match nothing on
    disk.
    """
    assert JobScope(f"2018{BACKSLASH}junho").parts == ("2018", "junho")


def test_the_empty_scope_is_the_whole_device() -> None:
    for value in ("", ".", "  "):
        assert JobScope(value).is_whole_device
        assert JobScope(value).parts == ()


def test_a_scope_with_no_argument_is_the_whole_device() -> None:
    assert JobScope().is_whole_device


@pytest.mark.parametrize(
    "value",
    [
        "D:",
        "D:/fotos",
        "C:fotos",
        "/fotos",
        f"{BACKSLASH}fotos",
        "//server/share/fotos",
        f"{BACKSLASH}{BACKSLASH}server{BACKSLASH}share",
    ],
)
def test_an_absolute_or_unc_scope_is_refused(value: str) -> None:
    """Each of these names a location outside the device the job is for.

    A drive letter is the exact datum RFC-027 removed from the database,
    and a UNC share is not on this disk at all.
    """
    with pytest.raises(InvalidJobScopeError):
        JobScope(value)


@pytest.mark.parametrize("value", ["..", "../fotos", "2018/../2019", "a/b/../.."])
def test_a_scope_containing_a_parent_reference_is_refused(value: str) -> None:
    """Refused before anything is resolved, so the disk need not be present.

    Resolving first and checking afterwards gives an escaping scope one
    chance to be read as a real path, and makes the refusal depend on the
    folder existing -- which it should not.
    """
    with pytest.raises(InvalidJobScopeError):
        JobScope(value)


def test_redundant_separators_and_dots_collapse() -> None:
    assert JobScope("2018//junho/./x").parts == ("2018", "junho", "x")


def test_a_pure_path_is_accepted_in_either_flavour() -> None:
    assert JobScope(PurePosixPath("2018/junho")).parts == ("2018", "junho")
    assert JobScope(PureWindowsPath("2018/junho")).parts == ("2018", "junho")


def test_a_non_path_value_is_refused() -> None:
    with pytest.raises(InvalidJobScopeError):
        JobScope(42)  # type: ignore[arg-type]


def test_containment_is_a_prefix_of_parts_not_of_text() -> None:
    """`2018` contains `2018/junho` and does not contain `2018b`.

    Text prefixes would say otherwise, and `2018b` is a different folder.
    """
    assert JobScope("2018").contains(JobScope("2018/junho"))
    assert not JobScope("2018").contains(JobScope("2018b"))
    assert JobScope("2018").contains(JobScope("2018"))
    assert JobScope().contains(JobScope("anything/at/all"))


def test_a_nested_scope_is_absorbed() -> None:
    """The case RFC-029 section 5.2 has to survive: overlapping requests."""
    assert normalize_scopes((JobScope("2018"), JobScope("2018/junho"))) == (
        JobScope("2018"),
    )


def test_absorption_does_not_depend_on_the_order_asked_for() -> None:
    assert normalize_scopes((JobScope("2018/junho"), JobScope("2018"))) == (
        JobScope("2018"),
    )


def test_duplicates_collapse() -> None:
    assert normalize_scopes((JobScope("2018"), JobScope("2018"))) == (JobScope("2018"),)


def test_disjoint_scopes_all_survive() -> None:
    normalized = normalize_scopes(
        (JobScope("2019"), JobScope("2018"), JobScope("2018b"))
    )

    assert normalized == (JobScope("2018"), JobScope("2018b"), JobScope("2019"))


def test_a_whole_device_scope_absorbs_everything() -> None:
    """And collapses to zero rows, which is how the schema already says it."""
    assert normalize_scopes((JobScope("2018"), JobScope(""))) == ()


def test_no_scopes_stays_no_scopes() -> None:
    assert normalize_scopes(()) == ()


def test_deeply_nested_chains_absorb_to_the_shallowest() -> None:
    normalized = normalize_scopes(
        (JobScope("a/b/c"), JobScope("a"), JobScope("a/b"), JobScope("b"))
    )

    assert normalized == (JobScope("a"), JobScope("b"))
