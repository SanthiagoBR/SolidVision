"""Filesystem discovery result for a single candidate image file."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from app.domain.value_objects.capture_date import CaptureDate


@dataclass(frozen=True)
class DiscoveredImageFile:
    """Filesystem metadata for an image file found by `FilesystemImageProvider`."""

    path: Path
    filename: str
    extension: str
    file_size: int
    file_modified_at: datetime.datetime
    capture_date: CaptureDate | None = None
    """What the file says about when it was taken, read during the scan.

    `None` means *not examined*: extraction was switched off
    (`Settings.extract_capture_date`), or the file could not be read at
    that moment. It is not the same as `CaptureDate.unknown()`, which means
    the file was read and has no date. One field rather than a
    `captured_at` / `capture_source` pair, because the two halves are one
    fact and `CaptureDate` is where the rules binding them live.

    Read here, in the scan, rather than in the embedding pipeline
    (RFC-028 section 6): the scan touches every file, including the ones
    the incremental check will skip, so an already-indexed photo gains a
    capture date without paying for inference.
    """
