from __future__ import annotations

import datetime
from pathlib import Path

from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp")


def test_discovers_supported_images(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")
    (tmp_path / "other.jpg").write_bytes(b"data")

    provider = FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS)
    discovered = list(provider.discover())

    filenames = {item.filename for item in discovered}
    assert filenames == {"photo", "other"}


def test_ignores_unsupported_files(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"data")
    (tmp_path / "notes.txt").write_bytes(b"data")
    (tmp_path / "archive.zip").write_bytes(b"data")

    provider = FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS)
    discovered = list(provider.discover())

    assert len(discovered) == 1
    assert discovered[0].filename == "photo"


def test_extension_matching_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "one.JPG").write_bytes(b"data")
    (tmp_path / "two.jpg").write_bytes(b"data")
    (tmp_path / "three.JpG").write_bytes(b"data")

    provider = FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS)
    discovered = list(provider.discover())

    assert {item.filename for item in discovered} == {"one", "two", "three"}
    assert all(item.extension == "jpg" for item in discovered)


def test_discovers_nested_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.png").write_bytes(b"data")
    (tmp_path / "shallow.png").write_bytes(b"data")

    provider = FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS)
    discovered = list(provider.discover())

    assert {item.filename for item in discovered} == {"deep", "shallow"}


def test_empty_directory_yields_nothing(tmp_path: Path) -> None:
    provider = FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS)

    assert list(provider.discover()) == []


def test_missing_root_directory_yields_nothing_without_raising(
    tmp_path: Path,
) -> None:
    provider = FilesystemImageProvider(
        tmp_path / "does-not-exist", SUPPORTED_EXTENSIONS
    )

    assert list(provider.discover()) == []


def test_metadata_fields_are_collected(tmp_path: Path) -> None:
    file_path = tmp_path / "photo.png"
    file_path.write_bytes(b"0123456789")

    provider = FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS)
    (discovered,) = list(provider.discover())

    assert isinstance(discovered, DiscoveredImageFile)
    assert discovered.path == file_path
    assert discovered.filename == "photo"
    assert discovered.extension == "png"
    assert discovered.file_size == 10
    assert isinstance(discovered.file_modified_at, datetime.datetime)
    assert discovered.file_modified_at.tzinfo is not None
