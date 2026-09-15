from __future__ import annotations

import datetime
import os
from pathlib import Path

from PIL import Image

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.exif_capture_date import (
    DATE_TIME_ORIGINAL,
    EXIF_IFD_POINTER,
)
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


class TestCaptureDate:
    """RFC-028 section 6: the capture date is read in the scan, beside `stat()`."""

    SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)

    @staticmethod
    def write_jpeg(path: Path, date_time_original: str | None) -> Path:
        exif = Image.Exif()
        if date_time_original is not None:
            exif.get_ifd(EXIF_IFD_POINTER)[DATE_TIME_ORIGINAL] = date_time_original
        Image.new("RGB", (8, 8)).save(path, "JPEG", exif=exif.tobytes())
        return path

    def test_the_exif_capture_date_is_discovered(self, tmp_path: Path) -> None:
        self.write_jpeg(tmp_path / "DJI_0042.jpg", "2018:07:14 15:32:05")

        (discovered,) = FilesystemImageProvider(
            tmp_path, SUPPORTED_EXTENSIONS
        ).discover()

        assert discovered.capture_date == CaptureDate(
            self.SHOT, CaptureSource.EXIF_ORIGINAL
        )

    def test_a_file_without_exif_is_examined_and_unknown(self, tmp_path: Path) -> None:
        self.write_jpeg(tmp_path / "exported.jpg", None)

        (discovered,) = FilesystemImageProvider(
            tmp_path, SUPPORTED_EXTENSIONS
        ).discover()

        assert discovered.capture_date == CaptureDate.unknown()

    def test_the_modification_time_never_becomes_the_capture_date(
        self, tmp_path: Path
    ) -> None:
        """The scan has `mtime` in hand; RFC-028 section 4 forbids using it."""
        path = self.write_jpeg(tmp_path / "copied.jpg", None)
        old = datetime.datetime(2018, 7, 14).timestamp()
        os.utime(path, (old, old))

        (discovered,) = FilesystemImageProvider(
            tmp_path, SUPPORTED_EXTENSIONS
        ).discover()

        assert discovered.capture_date is not None
        assert discovered.capture_date.captured_at is None

    def test_extraction_switched_off_leaves_files_unexamined(
        self, tmp_path: Path
    ) -> None:
        """Off is "not examined" (`None`), never "no date" (`unknown`).

        Collapsing the two would mark every file scanned with the switch
        off as permanently dateless, and no later scan would look again.
        """
        self.write_jpeg(tmp_path / "DJI_0042.jpg", "2018:07:14 15:32:05")

        (discovered,) = FilesystemImageProvider(
            tmp_path, SUPPORTED_EXTENSIONS, extract_capture_date=False
        ).discover()

        assert discovered.capture_date is None

    def test_a_corrupt_file_does_not_stop_the_scan(self, tmp_path: Path) -> None:
        """RFC-028 section 13: the scan never sees the exception."""
        (tmp_path / "a-corrupt.jpg").write_bytes(b"\xff\xd8\xff\xe1\x00\x10Exif\x00")
        self.write_jpeg(tmp_path / "b-fine.jpg", "2018:07:14 15:32:05")
        (tmp_path / "c-not-an-image.png").write_bytes(b"plain text")

        discovered = list(
            FilesystemImageProvider(tmp_path, SUPPORTED_EXTENSIONS).discover()
        )

        assert [item.filename for item in discovered] == [
            "a-corrupt",
            "b-fine",
            "c-not-an-image",
        ]
        assert discovered[0].capture_date == CaptureDate.unknown()
        assert discovered[1].capture_date == CaptureDate(
            self.SHOT, CaptureSource.EXIF_ORIGINAL
        )
        assert discovered[2].capture_date == CaptureDate.unknown()
