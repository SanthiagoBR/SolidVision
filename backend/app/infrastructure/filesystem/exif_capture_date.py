"""Read when a photograph was taken, from inside the file (RFC-028 section 4).

    DateTimeOriginal   ->  CaptureSource.EXIF_ORIGINAL
    DateTimeDigitized  ->  CaptureSource.EXIF_DIGITIZED
    nothing usable     ->  CaptureSource.UNKNOWN, with no date

**The filesystem is not consulted, and neither is EXIF `DateTime`.**
`st_mtime` is the date of the last copy for a collection that lives by being
copied between disks (RFC-028 section 2.1), and tag 0x0132 `DateTime` is
what editing software stamps on save -- the same modification time wearing
an EXIF name. Both would put a plausible, confident, wrong date into a
column called `captured_at`, and nothing downstream could tell. RFC-028
section 4 removed the `mtime` fallback from the draft on purpose; this
module is where that decision is either kept or quietly undone.

This is a unit of its own, rather than three lines inside the discovery
loop, because nearly every line in it exists to survive something real
files do: a tag that lives one IFD down from where it looks like it lives,
a placeholder of zeros, NUL padding, a truncated header, a file that
vanished between being listed and being opened.

Read-only. Nothing here writes EXIF, or anything else, to a file in the
collection (RFC-028 section 10).
"""

from __future__ import annotations

import datetime
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource

EXIF_IFD_POINTER = 0x8769
"""Tag in IFD0 pointing at the Exif sub-IFD.

`DateTimeOriginal` and `DateTimeDigitized` live in that sub-IFD, not in
IFD0. `Image.getexif()[0x9003]` therefore returns nothing for almost every
real photograph, and code written that way passes against hand-built
fixtures that put the tag in the wrong place and returns `unknown` for an
entire camera roll.
"""

DATE_TIME_ORIGINAL = 0x9003
DATE_TIME_DIGITIZED = 0x9004

EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
"""EXIF 2.3's `YYYY:MM:DD HH:MM:SS` -- colons in the date too, and no zone."""

_CHAIN: tuple[tuple[int, CaptureSource], ...] = (
    (DATE_TIME_ORIGINAL, CaptureSource.EXIF_ORIGINAL),
    (DATE_TIME_DIGITIZED, CaptureSource.EXIF_DIGITIZED),
)
"""The fallback chain, strongest claim first. Deliberately two links long."""


def read_capture_date(path: Path) -> CaptureDate | None:
    """Return the capture date recorded inside `path`, or `None` if unreadable.

    Three outcomes, and the difference between the last two is the point:

    - a `CaptureDate` with a date and an EXIF source;
    - `CaptureDate.unknown()` when the file was read and holds no usable
      date -- no EXIF at all (most PNG and BMP files, which is normal and
      not logged as anything), placeholder zeros, garbage in the tag, or
      bytes Pillow cannot parse. Reading the same bytes again gives the same
      answer, so the row is marked examined and a later scan leaves it
      alone;
    - `None` when the file could not be *read* -- deleted since it was
      listed, locked, permission denied, a disk that went away. That is not
      a fact about the file, and recording it as `unknown` would stop every
      future scan from looking again (RFC-028 section 4.2). `None` leaves
      the row unexamined, so the next scan retries.

    **Never raises** for anything a file on disk can do to it. The caller
    is a scan over 100,000 files, and one corrupt JPEG must not end it
    (RFC-028 section 13).

    Only the header is read. `Image.open()` is lazy -- it parses enough to
    know the format and size, and EXIF comes with the header -- and nothing
    here calls `load()`, converts, or resizes, which is the whole basis of
    RFC-028 section 6's claim that this is cheap next to inference.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return None

    with handle:
        try:
            tags = _exif_sub_ifd(handle)
        except OSError as exc:
            # Pillow reports a malformed file as a bare `OSError("...")`,
            # with no errno; the operating system reports a failed read
            # with one. Only the first is a property of the bytes.
            if exc.errno is not None:
                return None
            return CaptureDate.unknown()
        except Exception:
            # Wide on purpose, and only around the Pillow calls. A corrupt
            # file surfaces as `UnidentifiedImageError`, `SyntaxError`,
            # `struct.error`, `ValueError`, `KeyError` or
            # `DecompressionBombError` depending on where the damage is,
            # and every one of them means the same thing here.
            return CaptureDate.unknown()

    for tag, source in _CHAIN:
        captured_at = parse_exif_datetime(tags.get(tag))
        if captured_at is not None:
            return CaptureDate(captured_at=captured_at, source=source)
    return CaptureDate.unknown()


def _exif_sub_ifd(handle: BinaryIO) -> Mapping[int, object]:
    """Open the image lazily and return its Exif sub-IFD, possibly empty.

    Warnings are silenced for the duration, because every one Pillow
    raises here is already reflected in the return value. "Corrupt EXIF
    data" comes back as an empty or partial IFD, which the chain resolves
    to `unknown`; `DecompressionBombWarning` fires for large aerial frames
    that nothing here will decode. Left on, a scan of a damaged archive
    would print one line per file to stderr and report nothing a caller
    could act on.

    Beyond twice Pillow's pixel limit `open()` raises instead of warning,
    and such a file comes back `unknown` even if it carries EXIF -- a
    declared limitation for very large orthomosaics, which the backfill's
    `--force` can revisit.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(handle) as image:
            return dict(image.getexif().get_ifd(EXIF_IFD_POINTER))


def parse_exif_datetime(raw: object) -> datetime.datetime | None:
    """Parse one EXIF date string into a naive `datetime`, or `None`.

    `None` for anything that is not a usable date, so the caller simply
    moves to the next link of the chain:

    - absent, or not text at all;
    - the placeholder `0000:00:00 00:00:00` that cameras and exporters
      write when the clock was never set. Filtered by name, not left to
      `strptime` to reject: swallowing the parse error would give the
      right answer for the wrong reason, and the next person to loosen the
      parsing would turn it into year 0 -- which `datetime` cannot even
      represent;
    - blanks-and-colons (`"    :  :     :  :  "`), the other placeholder
      the EXIF specification permits for an unknown date;
    - anything else `strptime` rejects: an impossible month, a trailing
      zone suffix some software appends, truncated text.

    NUL and whitespace are stripped first. EXIF ASCII values are
    NUL-terminated and often padded to a fixed width, and Pillow hands
    back the terminator as part of the string.

    The result is naive, always. The format has no zone, and inventing
    one is the error RFC-028 section 5 exists to prevent.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None

    text = raw.strip("\x00 \t\r\n")
    if _is_placeholder(text):
        return None
    try:
        return datetime.datetime.strptime(text, EXIF_DATETIME_FORMAT)
    except ValueError:
        return None


def _is_placeholder(text: str) -> bool:
    """Whether `text` is one of the EXIF spellings of "no date"."""
    significant = text.replace(":", "").replace(" ", "")
    return not significant or set(significant) == {"0"}
