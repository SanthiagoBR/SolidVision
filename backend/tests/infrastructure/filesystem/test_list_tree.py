"""The listing of RFC-046 section 5.2: `Path.walk(on_error=...)` in place of `rglob`.

What the walk must keep from the `rglob` -- the same entries, so the same
order after the `sorted()` and the same checkpoint -- and what it must add:
the folders it could not read, which the `rglob` swallowed (section 2).

A failing `scandir()` is simulated by replacing `Path._scandir`, which is
what `walk()` calls per folder in the Python 3.12 of this installation; the
real permission denied of the pilot is the Windows-only test below.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_scope import JobScope
from app.domain.value_objects.root_listing import RootListing
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
    _list_tree,
    discovery_sort_key,
)

SUPPORTED = (".jpg",)
REAL_SCANDIR = Path._scandir  # type: ignore[attr-defined]


class Recording(IndexingObserver):
    def __init__(self) -> None:
        self.listings: list[RootListing] = []

    def listed(self, listing: RootListing) -> None:
        self.listings.append(listing)

    def discovered(self, files_seen: int, complete: bool) -> None:
        return None

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def should_stop(self) -> bool:
        return False


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """Spaces, capitals, nesting, a folder of thumbnails to exclude, and a link."""
    root = tmp_path / "disk"
    for relative in (
        "2018/junho/DJI_0001.JPG",
        "2018/junho/dji_0002.jpg",
        "2018b/x.jpg",
        "a b/Foto de Família.jpg",
        "B/y.jpg",
        "B/notes.txt",
        "cache/thumbs/ab/1.jpg",
        "loose.jpg",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    (root / "empty folder").mkdir()
    return root


def scan(root: Path, observer: IndexingObserver, **options: Any) -> list[Path]:
    provider = FilesystemImageProvider(
        root,
        SUPPORTED,
        extract_capture_date=False,
        extract_gps=False,
        observer=observer,
        excluded_directories=(root / "cache",),
        **options,
    )
    return [discovered.path for discovered in provider.discover()]


def failing_in(*names: str) -> Any:
    """A `_scandir` that refuses the folders called `names`, as a denied read does."""

    def scandir(self: Path) -> Any:
        if self.name in names:
            raise PermissionError(13, "Access is denied", str(self))
        return REAL_SCANDIR(self)

    return scandir


class TestTheSameEntriesAsTheRglob:
    def test_the_entries_are_the_rglobs(self, tree: Path) -> None:
        """Folders and files, without the root, the excluded folder included."""
        listed = _list_tree(tree)

        assert sorted(listed.entries, key=discovery_sort_key) == sorted(
            tree.rglob("*"), key=discovery_sort_key
        )
        assert listed.unreadable == []

    def test_the_files_are_the_entries_that_are_files(self, tree: Path) -> None:
        listed = _list_tree(tree)

        assert set(listed.files) == {path for path in listed.entries if path.is_file()}
        # The same objects as in `entries`: the scan asks by identity.
        assert listed.file_identities() <= {id(path) for path in listed.entries}

    def test_the_scan_yields_what_it_yielded_before(self, tree: Path) -> None:
        """The order of the files, which the checkpoint names, is unchanged."""
        expected = [
            path
            for path in sorted(tree.rglob("*"), key=discovery_sort_key)
            if path.is_file()
            and path.suffix.lower() == ".jpg"
            and not path.is_relative_to(tree / "cache")
        ]

        assert scan(tree, Recording()) == expected

    @pytest.mark.skipif(
        sys.platform != "win32", reason="the order is Windows' case-insensitive one"
    )
    def test_the_order_is_the_paths_and_not_the_texts(self, tree: Path) -> None:
        names = [path.relative_to(tree).as_posix() for path in scan(tree, Recording())]

        assert names == [
            "2018/junho/DJI_0001.JPG",
            "2018/junho/dji_0002.jpg",
            "2018b/x.jpg",
            "a b/Foto de Família.jpg",
            "B/y.jpg",
            "loose.jpg",
        ]


class TestAFolderThatCannotBeRead:
    def test_it_is_reported_and_its_subtree_is_not_listed(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "_scandir", failing_in("junho"))

        listed = _list_tree(tree)

        assert listed.unreadable == [tree / "2018" / "junho"]
        assert tree / "2018" / "junho" in listed.entries
        assert not any(
            path.is_relative_to(tree / "2018" / "junho")
            for path in listed.entries
            if path != tree / "2018" / "junho"
        )

    def test_the_rglob_swallowed_it(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pilot of section 2, simulated: no error, and the subtree gone."""
        monkeypatch.setattr(Path, "_scandir", failing_in("junho"))

        listed = list(tree.rglob("*"))

        assert tree / "2018" / "junho" / "DJI_0001.JPG" not in listed

    def test_the_whole_listing_carries_it(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "_scandir", failing_in("junho"))
        observer = Recording()

        scan(tree, observer)

        (listing,) = observer.listings
        assert listing.unreadable == ("2018/junho",)
        assert listing.files == (
            "2018b/x.jpg",
            "a b/Foto de Família.jpg",
            "B/y.jpg",
            "loose.jpg",
        )
        assert listing.file_count == 4

    def test_it_is_a_warning_with_its_name_in_any_scan(
        self,
        tree: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Of some folders too, where there is no listing to carry it."""
        monkeypatch.setattr(Path, "_scandir", failing_in("junho"))
        observer = Recording()

        with caplog.at_level(logging.WARNING):
            scan(tree, observer, scopes=(JobScope("2018"),))

        assert observer.listings == []
        assert any(
            "2018/junho" in record.getMessage() and record.levelno == logging.WARNING
            for record in caplog.records
        )

    def test_one_inside_an_excluded_folder_is_not_reported(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`System Volume Information` is unreadable on every disk, and excluded."""
        monkeypatch.setattr(Path, "_scandir", failing_in("thumbs"))
        observer = Recording()

        scan(tree, observer)

        assert observer.listings[0].unreadable == ()

    def test_an_unreadable_root_is_the_whole_disk(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "_scandir", failing_in("disk"))
        observer = Recording()

        assert scan(tree, observer) == []
        (listing,) = observer.listings
        assert listing.files == ()
        assert [JobScope(folder) for folder in listing.unreadable] == [JobScope()]


class _BreaksAfterOne:
    """A directory listing that yields one entry and then fails, mid-read."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._given = 0

    def __enter__(self) -> _BreaksAfterOne:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._inner.close()

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        return self

    def __next__(self) -> os.DirEntry[str]:
        if self._given == 1:
            raise OSError(5, "I/O error in the middle of a folder")
        self._given += 1
        entry: os.DirEntry[str] = next(self._inner)
        return entry


def test_an_error_in_the_middle_of_a_folder_ends_the_scan(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not given to `on_error`: it propagates, and the job fails (section 5.2)."""

    def scandir(self: Path) -> Any:
        inner = REAL_SCANDIR(self)
        return _BreaksAfterOne(inner) if self.name == "2018" else inner

    monkeypatch.setattr(Path, "_scandir", scandir)

    with pytest.raises(OSError, match="middle of a folder"):
        scan(tree, Recording())


@pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows'")
def test_a_folder_denied_for_real_is_reported(tmp_path: Path) -> None:
    """The pilot of section 2: `icacls /deny (RD)` on a folder with photos."""
    root = tmp_path / "disk"
    (root / "ok").mkdir(parents=True)
    (root / "ok" / "a.jpg").write_bytes(b"data")
    blocked = root / "bloqueada"
    (blocked / "sub").mkdir(parents=True)
    (blocked / "b.jpg").write_bytes(b"data")
    (blocked / "sub" / "c.jpg").write_bytes(b"data")
    user = os.environ.get("USERNAME", "")
    denied = subprocess.run(
        ["icacls", str(blocked), "/deny", f"{user}:(RD)"],
        capture_output=True,
        check=False,
    )
    if denied.returncode != 0:
        pytest.skip(f"icacls refused: {denied.stderr!r}")
    try:
        observer = Recording()
        discovered = scan(root, observer)
    finally:
        subprocess.run(
            ["icacls", str(blocked), "/remove:d", user],
            capture_output=True,
            check=False,
        )

    assert discovered == [root / "ok" / "a.jpg"]
    (listing,) = observer.listings
    assert listing.unreadable == ("bloqueada",)
    assert listing.files == ("ok/a.jpg",)


class TestTheComparisonsAsText:
    """`_is_excluded()` and `_posix()` compare text (RFC-046 section 16.12).

    Each must give, for every entry of a tree, what `pathlib` gives --
    `is_relative_to()` and `relative_to().as_posix()` -- which they replace
    because `pathlib` builds every parent of a path to answer.
    """

    @pytest.mark.parametrize(
        "excluded",
        [
            ("cache",),
            ("2018",),
            ("cache/thumbs", "B"),
            ("a b", "2018/junho"),
        ],
    )
    def test_excluded_is_is_relative_to(
        self, tree: Path, excluded: tuple[str, ...]
    ) -> None:
        directories = tuple(tree / name for name in excluded)
        provider = FilesystemImageProvider(
            tree, SUPPORTED, excluded_directories=directories
        )
        resolved = [directory.resolve() for directory in directories]

        for path in _list_tree(tree).entries:
            expected = any(path.is_relative_to(other) for other in resolved)
            assert provider._is_excluded(path) is expected, path

    def test_a_folder_is_not_inside_its_namesake_with_a_suffix(
        self, tree: Path
    ) -> None:
        """`2018` excluded leaves `2018b` alone: the separator is the rule."""
        provider = FilesystemImageProvider(
            tree, SUPPORTED, excluded_directories=(tree / "2018",)
        )

        assert provider._is_excluded(tree / "2018" / "junho" / "DJI_0001.JPG")
        assert provider._is_excluded(tree / "2018")
        assert not provider._is_excluded(tree / "2018b" / "x.jpg")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows folds case")
    def test_the_case_is_folded_as_windows_folds_it(self, tree: Path) -> None:
        provider = FilesystemImageProvider(
            tree, SUPPORTED, excluded_directories=(tree / "CACHE",)
        )

        assert provider._is_excluded(tree / "cache" / "thumbs" / "ab" / "1.jpg")

    @pytest.mark.parametrize("trailing", [False, True])
    def test_the_relative_text_is_relative_to(self, tree: Path, trailing: bool) -> None:
        root = Path(str(tree) + os.sep) if trailing else tree
        provider = FilesystemImageProvider(root, SUPPORTED)

        for path in _list_tree(root).entries:
            assert provider._posix(path) == path.relative_to(root).as_posix()
        assert provider._posix(root) == "."
