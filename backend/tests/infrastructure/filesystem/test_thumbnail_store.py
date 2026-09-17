"""The on-disk thumbnail cache (RFC-030 section 7.1)."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.domain.value_objects.image_id import ImageId
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore


def test_a_saved_thumbnail_is_found_again_from_its_location(tmp_path: Path) -> None:
    store = FilesystemThumbnailStore(tmp_path / "cache")
    image_id = ImageId(uuid.uuid4())

    location = store.save(image_id, b"jpeg bytes")

    path = store.locate(location)
    assert path is not None
    assert path.read_bytes() == b"jpeg bytes"
    assert path.is_relative_to((tmp_path / "cache").resolve())


def test_the_location_is_relative_and_sharded_by_the_id(tmp_path: Path) -> None:
    """Relative so the cache can move; sharded so no directory holds 100,000 files."""
    store = FilesystemThumbnailStore(tmp_path)
    image_id = ImageId(uuid.UUID("3f2a0000-0000-0000-0000-000000000000"))

    location = store.save(image_id, b"x")

    assert location == f"3f/{image_id.value}.jpg"
    assert not Path(location).is_absolute()


def test_saving_again_replaces_the_thumbnail_under_the_same_location(
    tmp_path: Path,
) -> None:
    """The id does not change when the photo does; the bytes behind it must."""
    store = FilesystemThumbnailStore(tmp_path)
    image_id = ImageId(uuid.uuid4())

    first = store.save(image_id, b"old picture")
    second = store.save(image_id, b"new picture")

    assert first == second
    path = store.locate(second)
    assert path is not None
    assert path.read_bytes() == b"new picture"


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    store = FilesystemThumbnailStore(tmp_path)
    image_id = ImageId(uuid.uuid4())

    store.save(image_id, b"x")
    store.save(image_id, b"y")

    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert [path.name for path in files] == [f"{image_id.value}.jpg"]


def test_a_location_whose_file_was_cleaned_away_is_not_found(tmp_path: Path) -> None:
    store = FilesystemThumbnailStore(tmp_path)
    location = store.save(ImageId(uuid.uuid4()), b"x")
    located = store.locate(location)
    assert located is not None

    located.unlink()

    assert store.locate(location) is None


def test_a_location_that_escapes_the_cache_is_refused(tmp_path: Path) -> None:
    """Defence in depth: a hand-edited row must not make the endpoint read anything."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not a thumbnail", encoding="utf-8")
    store = FilesystemThumbnailStore(tmp_path / "cache")
    (tmp_path / "cache").mkdir()

    assert store.locate("../secret.txt") is None
    assert store.locate(str(secret)) is None
