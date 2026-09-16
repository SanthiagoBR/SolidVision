"""Discovery order, and the resume that depends on it (RFC-029 section 10).

Every fixture here is named with an uppercase letter, a space, or a shared
prefix, and that is the whole design of the module. Those are exactly the
names where path order and string order disagree:

    sorted(Path)  ->  a/x.jpg, a/Z.jpg, a b/x.jpg, B/y.jpg
    sorted(str)   ->  B/y.jpg, a b/x.jpg, a/Z.jpg, a/x.jpg

A resume implemented as `relative_path > :checkpoint` -- in SQL or in
Python -- skips and repeats arbitrary files, and passes any suite whose
fixtures are lowercase and space-free. `test_path_order_and_string_order_
actually_disagree_here` fails if these names ever stop being the awkward
case, so the rest of the module cannot quietly become vacuous.
"""

from __future__ import annotations

from pathlib import Path, PurePath

import pytest

from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
    is_after_checkpoint,
)

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png")

AWKWARD_NAMES = [
    "a/x.jpg",
    "a/Z.jpg",
    "a b/x.jpg",
    "B/y.jpg",
    "a/nested/deep.jpg",
]
"""Uppercase, a space, and two directories sharing a prefix (`a`, `a b`)."""


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    """Lay the awkward names out on a real filesystem."""
    for name in AWKWARD_NAMES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"data")
    return tmp_path


def discovered(provider: FilesystemImageProvider, root: Path) -> list[str]:
    """Return what a scan produced, as root-relative posix text."""
    return [item.path.relative_to(root).as_posix() for item in provider.discover()]


def provider(root: Path, **kwargs: object) -> FilesystemImageProvider:
    return FilesystemImageProvider(
        root,
        SUPPORTED_EXTENSIONS,
        extract_capture_date=False,
        **kwargs,  # type: ignore[arg-type]
    )


def test_path_order_and_string_order_actually_disagree_here() -> None:
    """The guard that keeps this module honest.

    If these names ever stopped being the case where the two orders
    differ, every other test below would still pass while testing nothing
    at all.
    """
    by_path = [path.as_posix() for path in sorted(PurePath(n) for n in AWKWARD_NAMES)]
    by_string = sorted(AWKWARD_NAMES)

    assert by_path != by_string


def test_discovery_is_in_path_order(corpus: Path) -> None:
    found = discovered(provider(corpus), corpus)

    assert found == [
        path.as_posix() for path in sorted(PurePath(name) for name in AWKWARD_NAMES)
    ]


def test_discovery_order_is_the_same_on_a_second_run(corpus: Path) -> None:
    """The premise of the checkpoint, stated as a test (RFC-029 section 10).

    "Continue after X" means nothing if the second scan visits the files
    in a different order.
    """
    first = discovered(provider(corpus), corpus)
    second = discovered(provider(corpus), corpus)

    assert first == second


def test_resuming_yields_exactly_the_tail(corpus: Path) -> None:
    """And the tail is the path-order tail, not the string-order one.

    Resuming after `a/x.jpg`: in path order three files remain; a string
    comparison would say only `a b/x.jpg` and `a/Z.jpg` do, because
    `"B/y.jpg" < "a/x.jpg"` as text -- so it would silently drop `B/y.jpg`
    for good.
    """
    everything = discovered(provider(corpus), corpus)
    checkpoint = PurePath("a/x.jpg")

    remaining = discovered(provider(corpus, resume_after=checkpoint), corpus)

    assert remaining == everything[everything.index("a/x.jpg") + 1 :]
    assert "B/y.jpg" in remaining
    assert "a/x.jpg" not in remaining


def test_resuming_after_the_last_file_yields_nothing(corpus: Path) -> None:
    last = PurePath(discovered(provider(corpus), corpus)[-1])

    assert discovered(provider(corpus, resume_after=last), corpus) == []


def test_no_checkpoint_means_everything(corpus: Path) -> None:
    assert discovered(provider(corpus, resume_after=None), corpus) == discovered(
        provider(corpus), corpus
    )


def test_the_checkpointed_file_itself_is_not_repeated(corpus: Path) -> None:
    """The checkpoint is the last file made durable, so it is already done."""
    assert is_after_checkpoint(PurePath("a/x.jpg"), PurePath("a/x.jpg")) is False
    assert is_after_checkpoint(PurePath("a/Z.jpg"), PurePath("a/x.jpg")) is True


def test_a_scope_restricts_the_walk(corpus: Path) -> None:
    found = discovered(provider(corpus, scopes=(JobScope("a"),)), corpus)

    assert found == ["a/nested/deep.jpg", "a/x.jpg", "a/Z.jpg"]


def test_a_scope_that_shares_a_prefix_is_not_swept_in(corpus: Path) -> None:
    """`a` and `a b` are different folders, whatever a text prefix suggests."""
    found = discovered(provider(corpus, scopes=(JobScope("a"),)), corpus)

    assert "a b/x.jpg" not in found


def test_no_scopes_means_the_whole_root(corpus: Path) -> None:
    """Every caller written before RFC-029 keeps the behaviour it had."""
    assert discovered(provider(corpus, scopes=()), corpus) == discovered(
        provider(corpus), corpus
    )


def test_several_scopes_stay_in_global_path_order(corpus: Path) -> None:
    """The condition that lets a resume span scope boundaries.

    Normalised scopes are disjoint, so walking them in path order and each
    one internally in path order produces a sequence that is globally in
    path order -- which is the sequence the checkpoint comparison assumes.
    Hand the scopes over in the wrong order and this is what breaks.
    """
    scopes = (JobScope("B"), JobScope("a"), JobScope("a b"))

    found = discovered(provider(corpus, scopes=scopes), corpus)

    assert found == [PurePath(name).as_posix() for name in found]
    assert found == sorted(found, key=PurePath)
    assert found == discovered(provider(corpus), corpus)


def test_resuming_works_across_a_scope_boundary(corpus: Path) -> None:
    scopes = (JobScope("a"), JobScope("a b"), JobScope("B"))

    remaining = discovered(
        provider(corpus, scopes=scopes, resume_after=PurePath("a/Z.jpg")), corpus
    )

    assert remaining == ["a b/x.jpg", "B/y.jpg"]


def test_a_scope_that_is_not_on_disk_yields_nothing(corpus: Path) -> None:
    """Not an error here. Whether a scope exists is validated at creation.

    The executor re-validates when it claims the job, because a disk
    plugged in at `POST` can be in a drawer by the time the job runs.
    """
    assert discovered(provider(corpus, scopes=(JobScope("nope"),)), corpus) == []


def test_a_missing_root_yields_nothing(tmp_path: Path) -> None:
    """Unchanged from before RFC-029, and deliberately still not an error."""
    assert discovered(provider(tmp_path / "gone"), tmp_path / "gone") == []
