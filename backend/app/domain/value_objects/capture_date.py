"""An examined capture date: the value and where it came from (RFC-028).

`captured_at` and `capture_source` are two columns and two fields on
`Image`, but they are one fact, and the rules that bind them are the kind
that break silently when each half is written on its own: a date with no
source, a source claiming EXIF with no date, or a date carrying a time
zone the file never recorded. This value object is where those rules are
enforced once, and it is what the extraction produces and what the
repository port accepts when it writes a date without touching anything
else.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.exceptions import InvalidCaptureDateError
from app.domain.value_objects.capture_source import CaptureSource


@dataclass(frozen=True)
class CaptureDate:
    """The outcome of examining one file for when it was taken.

    Always the result of an examination. "Never examined" is not a
    `CaptureDate` at all -- it is the *absence* of one, `None` wherever a
    `CaptureDate | None` is expected -- and that asymmetry is the NULL /
    `'unknown'` distinction of RFC-028 section 4.2 expressed as a type.
    """

    captured_at: datetime.datetime | None
    """The camera's local wall-clock time, with no time zone. Never aware.

    EXIF `DateTimeOriginal` is `YYYY:MM:DD HH:MM:SS` and nothing else: a
    reading of the camera clock, not an instant. Attaching any zone -- UTC,
    or the zone of the machine that happened to run the scan -- invents
    information the file does not contain and moves New Year's Eve shots
    across the year boundary (RFC-028 section 5).
    """

    source: CaptureSource

    def __post_init__(self) -> None:
        validate_capture_fields(self.captured_at, self.source)

    @classmethod
    def unknown(cls) -> CaptureDate:
        """The file was examined and holds no usable date."""
        return cls(captured_at=None, source=CaptureSource.UNKNOWN)


def validate_capture_fields(
    captured_at: datetime.datetime | None, source: CaptureSource | None
) -> None:
    """Enforce the pairing rules for a capture date held as two loose fields.

    Shared by `CaptureDate` and `Image`, which stores the two halves
    separately because they are two columns. `source is None` is legal
    here and nowhere in `CaptureDate`: on `Image` it means the row was
    never examined, and then there can be no date either.

    The time-zone check is the one with teeth. A zone-aware value compared
    with a `TIMESTAMP WITHOUT TIME ZONE` column fails inside PostgreSQL,
    and compared with a naive value in Python raises `TypeError` -- two
    different failures for the same call, which is what the shared
    repository contract test exists to rule out. Rejecting it at
    construction makes both impossible.
    """
    if captured_at is not None and captured_at.tzinfo is not None:
        raise InvalidCaptureDateError(
            f"A capture date is the camera's local time and carries no time "
            f"zone; got {captured_at.isoformat()}."
        )
    if source is None:
        if captured_at is not None:
            raise InvalidCaptureDateError(
                "A capture date needs the source it was read from."
            )
        return
    if not isinstance(source, CaptureSource):
        raise InvalidCaptureDateError("Capture source must be a CaptureSource member.")
    if (captured_at is None) != (source is CaptureSource.UNKNOWN):
        raise InvalidCaptureDateError(
            f"Capture source {source.value!r} does not match "
            f"{'a missing' if captured_at is None else 'a present'} date: "
            f"'unknown' is exactly the case with no date."
        )
