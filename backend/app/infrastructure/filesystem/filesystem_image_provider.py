"""Infrastructure filesystem abstraction for discovering candidate image files."""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Iterator
from pathlib import Path

from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile
from app.infrastructure.filesystem.exif_capture_date import read_capture_date


class FilesystemImageProvider:
    """Recursively discovers supported image files under a root directory.

    Knows nothing about repositories, embeddings, PostgreSQL, workers, or
    use cases -- it only reads the filesystem via the standard library and,
    since RFC-028, each file's EXIF header via Pillow.
    """

    def __init__(
        self,
        root: Path,
        supported_extensions: Iterable[str],
        extract_capture_date: bool = True,
    ) -> None:
        """Configure a scan of `root`.

        `extract_capture_date` is a constructor argument rather than a
        read of `settings`, keeping this class free of configuration: the
        composition root passes `settings.extract_capture_date` in. It
        defaults to on because RFC-028 section 6 places extraction in the
        scan, and it exists at all because section 11 names the one reason
        to turn it off -- a measured per-file cost too high for a very
        large collection.
        """
        self._root = root
        self._supported_extensions = {ext.lower() for ext in supported_extensions}
        self._extract_capture_date = extract_capture_date

    def discover(self) -> Iterator[DiscoveredImageFile]:
        """Yield metadata for every supported image file under the root directory.

        The capture date is read in the same pass as `stat()`, not in a
        second walk and not lazily. A lazy field -- a callable on
        `DiscoveredImageFile` -- was considered and deferred until a
        measurement asks for it (RFC-028 section 11): it would move the
        read out of the scan and into whichever consumer happened to call
        it first, which is how a cost stops being visible.
        """
        if not self._root.exists():
            return

        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self._supported_extensions:
                continue

            stat = path.stat()
            yield DiscoveredImageFile(
                path=path,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                file_size=stat.st_size,
                file_modified_at=datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ),
                capture_date=(
                    read_capture_date(path) if self._extract_capture_date else None
                ),
            )
