"""Where an image's position came from (RFC-032 section 4.4).

The same three-state design as `CaptureSource` (RFC-028 section 4.2), applied
to the second fact the scan reads from the same header. With it, "what
fraction of the collection has a position?" is a `GROUP BY` rather than a
re-read of every file, and a backfill knows which rows are worth revisiting.
"""

from __future__ import annotations

import enum


class PositionSource(enum.StrEnum):
    """Where `latitude`/`longitude` were read from, or that nothing was.

    A `StrEnum` persisted into a plain `String` column rather than a native
    PostgreSQL `ENUM`. Two future members already have names and are
    deliberately **not** members yet: `'manual'` -- the user marks the point
    on the map -- and `'subject_estimated'` -- the photographed point
    computed from gimbal and altitude (RFC-032 sections 9 and 10). Each has
    a writer to build first, and adding either must not cost a migration.

    **The absence of a value is not a member.** A row whose source is NULL
    was *never examined* -- it predates RFC-032, or was scanned with
    `extract_gps` off -- while `UNKNOWN` means it *was* examined and holds
    no usable position. The two must not collapse: a scan writes only NULL
    rows, which is what keeps re-scanning an unchanged collection free of
    writes, and files without GPS are numerous in exactly the collection
    this RFC serves.

    **There is no XMP member.** RFC-032 section 2.3 measured 40 real DJI
    files carrying a `drone-dji` XMP packet and found no latitude or
    longitude in any of them; a member for a source that was never seen to
    hold a position would be a fallback that never fires.
    """

    EXIF_GPS = "exif_gps"
    """The GPS IFD, pointed at by tag 0x8825 in IFD0.

    The position of the *aircraft*, not of what it photographed (RFC-032
    section 2.2). The search radius absorbs the difference; nothing here
    corrects it.
    """

    UNKNOWN = "unknown"
    """The file was examined and carries no usable position.

    Pairs with `latitude = longitude = NULL`. An image with this source
    never matches a search circle, however large (RFC-020).
    """

    @property
    def precedence(self) -> int:
        """Rank within the chain; higher is a stronger claim.

        Exists for the backfill's `--force` rule, as `CaptureSource`'s
        does: re-examining a row may replace its source only with one at
        least as strong, so a file that fails to open today cannot turn a
        stored `exif_gps` into `unknown` (RFC-032 section 8.1). Separate
        from the capture-date chain on purpose -- a file can lose its date
        and keep its position, and each fact is judged on its own.
        """
        return _PRECEDENCE[self]


_PRECEDENCE = {
    PositionSource.EXIF_GPS: 1,
    PositionSource.UNKNOWN: 0,
}
