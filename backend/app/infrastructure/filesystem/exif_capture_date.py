"""Read when and where a photograph was taken, from inside the file (RFC-028, RFC-032).

    DateTimeOriginal   ->  CaptureSource.EXIF_ORIGINAL
    DateTimeDigitized  ->  CaptureSource.EXIF_DIGITIZED
    nothing usable     ->  CaptureSource.UNKNOWN, with no date

    GPS IFD (0x8825)   ->  PositionSource.EXIF_GPS
    nothing usable     ->  PositionSource.UNKNOWN, with no position

**One open, two facts** (RFC-032 section 4.2). The scan already opens every
candidate to read its date, and opening it again for the GPS would double the
measured cost -- 0.39 ms becoming ~0.8 ms a file, ~40 s becoming ~80 s per
100,000 files -- to reread the same header bytes. `read_exif_facts()` opens
once and returns both; `read_capture_date()` remains as the date's reader.

**The two facts are independent; neither is derived from the other.** Each
keeps the three outcomes RFC-028 section 4.3 established -- a value, `unknown`,
or `None` for "not examined" -- *per fact*. A file that cannot be read is not
examined for either; a file read without GPS and with a date is `unknown` for
its position and dated for its date; a GPS block too damaged to parse makes the
position `unknown` and leaves the date alone.

**The filesystem is not consulted, and neither is EXIF `DateTime`.**
`st_mtime` is the date of the last copy for a collection that lives by being
copied between disks (RFC-028 section 2.1), and tag 0x0132 `DateTime` is
what editing software stamps on save -- the same modification time wearing
an EXIF name. **And there is no XMP fallback for the position**: RFC-032
section 2.3 found the `drone-dji` XMP packet in all 40 real DJI files it read,
and no latitude or longitude in any of them.

A unit of its own, rather than lines inside the discovery loop, because
nearly every line in it exists to survive something real files do: a tag
that lives one IFD away from where it looks like it lives, a placeholder of
zeros, NUL padding, a hemisphere letter that went missing, a truncated header,
a file that vanished between being listed and being opened.

Read-only. Nothing here writes EXIF, or anything else, to a file in the
collection (RFC-028 section 10, RFC-032 section 10).
"""

from __future__ import annotations

import datetime
import math
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.position import (
    MAX_LATITUDE,
    MAX_LONGITUDE,
    Position,
    PositionReading,
)
from app.domain.value_objects.position_source import PositionSource

EXIF_IFD_POINTER = 0x8769
"""Tag in IFD0 pointing at the Exif sub-IFD.

`DateTimeOriginal` and `DateTimeDigitized` live in that sub-IFD, not in
IFD0. `Image.getexif()[0x9003]` therefore returns nothing for almost every
real photograph, and code written that way passes against hand-built
fixtures that put the tag in the wrong place and returns `unknown` for an
entire camera roll.
"""

GPS_IFD_POINTER = 0x8825
"""Tag in IFD0 pointing at the GPS IFD -- not the Exif sub-IFD, one IFD over.

The same trap as `EXIF_IFD_POINTER`, next door (RFC-032 section 4.1): the
coordinates are neither in IFD0 nor in the sub-IFD the date comes from, and
a reader that looked in either would pass every test built the same wrong
way and return `unknown` for 95% of the collection.
"""

GPS_LATITUDE_REF = 0x0001
GPS_LATITUDE = 0x0002
GPS_LONGITUDE_REF = 0x0003
GPS_LONGITUDE = 0x0004
"""A coordinate is **four** values, not two.

`GPSLatitude` and `GPSLongitude` are degree/minute/second triples of
unsigned rationals; the *sign* lives in `GPSLatitudeRef` (`N`/`S`) and
`GPSLongitudeRef` (`E`/`W`). Dropping the reference raises no error -- it
puts a photo from Parana in the northern hemisphere.
"""

DATE_TIME_ORIGINAL = 0x9003
DATE_TIME_DIGITIZED = 0x9004

EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
"""EXIF 2.3's `YYYY:MM:DD HH:MM:SS` -- colons in the date too, and no zone."""

NULL_ISLAND_TOLERANCE_DEGREES = 1e-4
"""How close to `0, 0` counts as the "no satellite fix" placeholder: ~11 m.

Not exact equality, because a partial fix writes zeros with noise in them
(RFC-032 section 4.3). The point it guards is real -- the Gulf of Guinea,
"Null Island", where coordinate errors pile up in databases worldwide -- and
"every photo without a fix, in one spot in the ocean" is the kind of answer
that looks right until someone clicks on it.
"""

_CHAIN: tuple[tuple[int, CaptureSource], ...] = (
    (DATE_TIME_ORIGINAL, CaptureSource.EXIF_ORIGINAL),
    (DATE_TIME_DIGITIZED, CaptureSource.EXIF_DIGITIZED),
)
"""The fallback chain, strongest claim first. Deliberately two links long."""

_HEMISPHERE_SIGNS: tuple[Mapping[str, int], Mapping[str, int]] = (
    {"N": 1, "S": -1},
    {"E": 1, "W": -1},
)


@dataclass(frozen=True)
class ExifFacts:
    """The two facts one header read yields, each examined or not on its own.

    `None` in a field means *not examined* -- switched off, or the file
    could not be read -- exactly as `DiscoveredImageFile` uses it. A value
    means examined, which includes the `unknown` outcome.
    """

    capture_date: CaptureDate | None
    position: PositionReading | None


def read_exif_facts(
    path: Path, capture_date: bool = True, position: bool = True
) -> ExifFacts:
    """Open `path` once and read the requested facts from its header.

    Per fact, three outcomes, and the difference between the last two is
    the point:

    - a value with an EXIF source;
    - `unknown` when the file was read and holds nothing usable -- no EXIF
      at all (normal for PNG and BMP, and not logged as anything), a
      placeholder, garbage in a tag, half a coordinate, or bytes Pillow
      cannot parse. Reading the same bytes again gives the same answer, so
      the row is marked examined and a later scan leaves it alone;
    - `None` when the file could not be *read* -- deleted since it was
      listed, locked, a disk that went away. That is not a fact about the
      file, and recording it as `unknown` would stop every future scan from
      looking again. `None` leaves the row unexamined, so the next scan
      retries.

    A fact that was not requested is `None` -- not examined -- whatever the
    file holds: switched off means "nobody looked", never "nothing there"
    (`Settings.extract_capture_date`, `Settings.extract_gps`).

    **Never raises** for anything a file on disk can do to it. The caller
    is a scan over 100,000 files, and one corrupt JPEG must not end it
    (RFC-028 section 13).

    Only the header is read: `Image.open()` is lazy, and nothing here calls
    `load()`, converts or resizes.
    """
    if not capture_date and not position:
        return ExifFacts(capture_date=None, position=None)

    try:
        handle = path.open("rb")
    except OSError:
        return ExifFacts(capture_date=None, position=None)

    with handle:
        try:
            sub_ifd, gps_ifd = _exif_ifds(handle)
        except OSError as exc:
            # Pillow reports a malformed file as a bare `OSError("...")`,
            # with no errno; the operating system reports a failed read
            # with one. Only the first is a property of the bytes.
            if exc.errno is not None:
                return ExifFacts(capture_date=None, position=None)
            sub_ifd, gps_ifd = {}, {}
        except Exception:
            # Wide on purpose, and only around the Pillow calls. A corrupt
            # file surfaces as `UnidentifiedImageError`, `SyntaxError`,
            # `struct.error`, `ValueError`, `KeyError` or
            # `DecompressionBombError` depending on where the damage is,
            # and every one of them means the same thing here: examined,
            # nothing usable.
            sub_ifd, gps_ifd = {}, {}

    return ExifFacts(
        capture_date=_capture_date(sub_ifd) if capture_date else None,
        position=_position(gps_ifd) if position else None,
    )


def read_capture_date(path: Path) -> CaptureDate | None:
    """Return the capture date recorded inside `path`, or `None` if unreadable.

    The date's reader, kept as RFC-028 shipped it and now a view of
    `read_exif_facts()`, so the two can never disagree about a file. The
    scan calls `read_exif_facts()` directly: calling this and a position
    reader separately would open every file twice.
    """
    return read_exif_facts(path, capture_date=True, position=False).capture_date


def _exif_ifds(handle: BinaryIO) -> tuple[Mapping[int, object], Mapping[int, object]]:
    """Open the image lazily, once, and return its Exif sub-IFD and its GPS IFD.

    **Each IFD is read on its own**, so damage to one cannot take the other
    fact with it: a GPS block Pillow fails to parse comes back empty --
    `unknown` position -- while the date is still read from its own IFD
    (RFC-032 section 4.2). A failure with an errno is the disk, not the
    bytes, and is let through so the caller can answer "not examined".

    Warnings are silenced for the duration, because every one Pillow raises
    here is already reflected in the return value. "Corrupt EXIF data"
    comes back as an empty or partial IFD, which resolves to `unknown`;
    `DecompressionBombWarning` fires for large aerial frames that nothing
    here will decode.

    Beyond twice Pillow's pixel limit `open()` raises instead of warning,
    and such a file comes back `unknown` for both facts even if it carries
    EXIF -- a declared limitation for very large orthomosaics, which the
    backfill's `--force` can revisit.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(handle) as image:
            exif = image.getexif()
            return _ifd(exif, EXIF_IFD_POINTER), _ifd(exif, GPS_IFD_POINTER)


def _ifd(exif: Image.Exif, pointer: int) -> Mapping[int, object]:
    """One IFD as a plain dict, or empty if it is absent or will not parse."""
    try:
        return dict(exif.get_ifd(pointer))
    except OSError as exc:
        if exc.errno is not None:
            raise
        return {}
    except Exception:
        return {}


def _capture_date(sub_ifd: Mapping[int, object]) -> CaptureDate:
    """Walk the RFC-028 chain over the Exif sub-IFD; `unknown` at its end."""
    for tag, source in _CHAIN:
        captured_at = parse_exif_datetime(sub_ifd.get(tag))
        if captured_at is not None:
            return CaptureDate(captured_at=captured_at, source=source)
    return CaptureDate.unknown()


def _position(gps_ifd: Mapping[int, object]) -> PositionReading:
    """Turn a GPS IFD into a reading, applying RFC-032 section 4.3 in order.

    Every refusal below is `unknown`, and each is a separate line so that
    none of them can be "simplified" into another:

    - no GPS IFD, or no latitude, or no longitude -- **never half a
      position**;
    - a reference that is missing or is not `N`/`S` (`E`/`W`): the
      hemisphere is never guessed;
    - a triple that is not three numbers, a rational with a zero
      denominator, `NaN`, or a negative component;
    - the `0, 0` placeholder, **recognised by name** rather than left for a
      range check to reject by accident -- `0, 0` is inside every range;
    - anything outside `[-90, 90]` x `[-180, 180]`.
    """
    latitude = parse_gps_coordinate(
        gps_ifd.get(GPS_LATITUDE), gps_ifd.get(GPS_LATITUDE_REF), _HEMISPHERE_SIGNS[0]
    )
    longitude = parse_gps_coordinate(
        gps_ifd.get(GPS_LONGITUDE),
        gps_ifd.get(GPS_LONGITUDE_REF),
        _HEMISPHERE_SIGNS[1],
    )
    if latitude is None or longitude is None:
        return PositionReading.unknown()
    if is_null_island(latitude, longitude):
        return PositionReading.unknown()
    if abs(latitude) > MAX_LATITUDE or abs(longitude) > MAX_LONGITUDE:
        return PositionReading.unknown()
    return PositionReading(
        position=Position(latitude, longitude), source=PositionSource.EXIF_GPS
    )


def parse_gps_coordinate(
    raw: object, reference: object, signs: Mapping[str, int]
) -> float | None:
    """Decimal degrees from a DMS triple and its hemisphere letter, or `None`.

    `signs` maps the two legal letters for this axis to their sign. The
    letter is ASCII in the file and Pillow may hand it back as `str` or
    `bytes`, NUL-terminated either way.
    """
    sign = _hemisphere_sign(reference, signs)
    if sign is None:
        return None
    if not isinstance(raw, tuple | list) or len(raw) != 3:
        return None
    try:
        degrees, minutes, seconds = (float(part) for part in raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    parts = (degrees, minutes, seconds)
    if not all(math.isfinite(part) and part >= 0 for part in parts):
        return None
    return sign * (degrees + minutes / 60 + seconds / 3600)


def is_null_island(latitude: float, longitude: float) -> bool:
    """Whether a coordinate is the receivers' "no fix" placeholder, `0, 0`."""
    return (
        abs(latitude) < NULL_ISLAND_TOLERANCE_DEGREES
        and abs(longitude) < NULL_ISLAND_TOLERANCE_DEGREES
    )


def _hemisphere_sign(reference: object, signs: Mapping[str, int]) -> int | None:
    if isinstance(reference, bytes):
        try:
            reference = reference.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(reference, str):
        return None
    return signs.get(reference.strip("\x00 \t\r\n").upper())


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
