"""Infrastructure filesystem abstraction for discovering candidate image files."""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Iterator
from pathlib import Path

from app.infrastructure.filesystem.discovered_image_file import DiscoveredImageFile


class FilesystemImageProvider:
    """Recursively discovers supported image files under a root directory.

    Knows nothing about repositories, embeddings, PostgreSQL, workers, or
    use cases -- it only reads the filesystem via the standard library.
    """

    def __init__(self, root: Path, supported_extensions: Iterable[str]) -> None:
        self._root = root
        self._supported_extensions = {ext.lower() for ext in supported_extensions}

    def discover(self) -> Iterator[DiscoveredImageFile]:
        """Yield metadata for every supported image file under the root directory."""
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
            )
