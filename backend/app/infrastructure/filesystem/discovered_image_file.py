"""Filesystem discovery result for a single candidate image file."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredImageFile:
    """Filesystem metadata for an image file found by `FilesystemImageProvider`."""

    path: Path
    filename: str
    extension: str
    file_size: int
    file_modified_at: datetime.datetime
