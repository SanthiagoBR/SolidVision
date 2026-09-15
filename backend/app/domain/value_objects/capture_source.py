"""Where an image's capture date came from (RFC-028 section 4.2).

Without this, `captured_at` would be a timestamp whose reliability varies
row by row and cannot be observed. With it, a caller can tell a date the
camera wrote at the instant of the shot from one a scanner wrote at
digitisation, a future backfill knows which rows are worth revisiting, and
"what fraction of the collection has a known date?" is a `GROUP BY` rather
than a re-read of every file.
"""

from __future__ import annotations

import enum


class CaptureSource(enum.StrEnum):
    """One link of the RFC-028 section 4 fallback chain, or the end of it.

    A `StrEnum` persisted into a plain `String` column rather than a native
    PostgreSQL `ENUM`. Adding a member -- `gps_derived`, `filename_parsed`,
    the sources RFC-028 section 11 anticipates -- would then cost a
    migration for no benefit a closed database type actually provides here.

    **There is no `mtime` member, and there must never be one.** RFC-028
    section 4 is a deliberate reversal of the draft that ended the chain on
    the filesystem timestamp: `mtime` is systematically wrong for a
    collection whose life consists of being copied from disk to disk
    (RFC-028 section 2.1), and a member for it would make that wrong date
    look like one more legitimate source.

    **The absence of a value is not a member either.** A row whose source
    is `NULL` was *never examined* -- it predates RFC-028, or it was
    scanned with extraction switched off -- while `UNKNOWN` means it *was*
    examined and holds no date at all. The two must not collapse: a scan
    rereads the first kind and leaves the second alone, which is what keeps
    re-scanning an unchanged collection free of writes (RFC-028 section
    4.2).
    """

    EXIF_ORIGINAL = "exif_original"
    """EXIF `DateTimeOriginal` (0x9003): the camera clock at the shot."""

    EXIF_DIGITIZED = "exif_digitized"
    """EXIF `DateTimeDigitized` (0x9004): when the image became digital.

    For a digital camera the two are normally identical. They differ for a
    scanned print or a film negative, where this is the date of the scan
    -- still a fact written into the file, which is what separates it from
    `mtime`, but a weaker claim about when the photograph was taken.
    """

    UNKNOWN = "unknown"
    """The file was examined and carries no usable date.

    Pairs with `captured_at = NULL`. An image with this source never
    matches a date-range filter (RFC-028 section 4.1).
    """

    @property
    def precedence(self) -> int:
        """Rank within the fallback chain; higher is a stronger claim.

        Exists for one rule, which is easy to get destructively wrong:
        re-examining a row (the backfill's `--force`) may replace a source
        only with one at least as strong. A naive "re-read everything and
        write what came back" would overwrite `exif_original` with
        `unknown` the first time a file failed to open -- and would pass
        every test that only ever re-reads healthy files.
        """
        return _PRECEDENCE[self]


_PRECEDENCE = {
    CaptureSource.EXIF_ORIGINAL: 2,
    CaptureSource.EXIF_DIGITIZED: 1,
    CaptureSource.UNKNOWN: 0,
}
