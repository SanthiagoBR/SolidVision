"""Filesystem discovery result for a single candidate image file."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.position import PositionReading


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

    position: PositionReading | None = None
    """What the file says about where it was taken, read in the same open (RFC-032).

    The same semantics of `None` as `capture_date`: *not examined* --
    `Settings.extract_gps` is off, or the file could not be read -- which
    is not `PositionReading.unknown()`, the file read and found without a
    usable position. The two are filled by one `read_exif_facts()` call, so
    a file costs one header open whichever of them is switched on, and they
    are still independent: either can be `None` or `unknown` while the
    other holds a value.
    """
