"""Image entity for the domain layer."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.exceptions import DeviceNotConnectedError
from app.domain.value_objects.capture_date import CaptureDate, validate_capture_fields
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath


@dataclass(frozen=True)
class Image:
    """Immutable business entity representing an image known by the system.

    RFC-027 replaced the single absolute `path` with the pair
    `(device_id, relative_path)`. The absolute path was not a description
    of where the file is; it was a description of where the file was *the
    last time somebody looked*, because a removable volume's drive letter
    is assigned by mount order. Storing it meant a stationary file's
    recorded location changed on its own, taking the derived `ImageId`
    with it (RFC-027 section 2.1).

    The pair does not have that problem. A device id is derived from an
    identifier the operating system keeps stable across remounts, and a
    path relative to the device's mount point does not contain the part
    that moves.
    """

    id: ImageId
    device_id: DeviceId
    relative_path: ImagePath
    """Where the file sits *within its device*, e.g. `fotos/2018/DJI_0042.JPG`.

    Never begins with a drive letter or a mount point. Joining this onto
    a mount point resolved at the moment of use is how an absolute path
    is obtained -- see `absolute_path` below.
    """

    filename: str
    extension: str

    absolute_path: ImagePath | None = None
    """Where the file is right now, when that is currently knowable.

    Computed, never persisted, and therefore never part of what makes two
    images equal. `None` is a first-class answer with a precise meaning:
    *this image's device is not mounted, so it has no absolute path at
    this instant.* That is exactly the case RFC-027 section 2.3 exists to
    represent -- a search hit on a disk in a drawer is a useful answer
    ("it is on HD3"), not a broken one, and it would be a lie to hand it a
    path that resolves to nothing.

    Set by whoever resolved the mount point: the indexing worker, which
    discovered the file on a connected disk, and later RFC-030, which
    resolves it per request for the search response. A component that
    needs the bytes -- the content hasher, the CLIP adapter -- reads this
    and must fail loudly when it is `None` rather than reconstruct a path
    of its own from the relative one.
    """

    captured_at: datetime.datetime | None = None
    """When the photograph was taken, as the camera's clock read it.

    Naive, always: EXIF records local wall-clock time with no zone, and
    this field keeps it that way from extraction to the HTTP response
    (RFC-028 section 5). `None` means unknown -- the file was never
    examined, or was examined and had no date -- and an image with an
    unknown date never matches a date-range filter (RFC-028 section 4.1).

    Here on the entity, unlike `file_size`, `file_modified_at` and
    `content_hash`, and the difference is what each one is *for*. Those
    three are change signals: only the incremental skip decision reads
    them, so they travel in `IndexMetadata` and `IndexingRecord`. A capture
    date is a searchable attribute of the photograph. It has to reach the
    search response through `SearchHit`, and it has to be visible to the
    filter predicate the in-memory repositories evaluate against an
    `Image`. Keeping it out of the entity would force those doubles to
    carry a side dictionary just to filter -- a second representation of
    the same fact, which is the divergence the shared contract test
    exists to prevent.

    The same argument `SearchHit` makes for keeping `similarity` *off*
    this entity cuts the other way here: a similarity means nothing
    without its query, while a capture date is the same for every caller.

    Not part of equality, which stays by `id`, like every other field.
    """

    capture_source: CaptureSource | None = None
    """Where `captured_at` came from; `None` if the file was never examined.

    `None` and `CaptureSource.UNKNOWN` are different answers, and the scan
    depends on the difference: `None` rows get their date written on the
    next scan that sees them, `UNKNOWN` rows are left alone because
    re-reading a file that had no date yields no date (RFC-028 section
    4.2).
    """

    def __post_init__(self) -> None:
        validate_capture_fields(self.captured_at, self.capture_source)

    @property
    def capture_date(self) -> CaptureDate | None:
        """The examined capture date as one value, or `None` if never examined."""
        if self.capture_source is None:
            return None
        return CaptureDate(captured_at=self.captured_at, source=self.capture_source)

    @property
    def display_path(self) -> ImagePath:
        """The most useful path available, for a log line or an error report.

        The absolute one when the device is mounted, the device-relative
        one otherwise. Never raises, because the callers are error paths:
        a run that is already reporting a failed file must not fail again
        while naming it.

        Distinct from `require_absolute_path()` on purpose. This one is
        for a human to read; that one is for opening a file, and the two
        must not be confused -- a component that fed `display_path` to
        `open()` would, for an unmounted device, read a relative path
        against the process\'s working directory.
        """
        return self.absolute_path or self.relative_path

    def require_absolute_path(self) -> ImagePath:
        """Return `absolute_path`, or raise if the device is not mounted.

        One place to fail, rather than one per adapter that needs bytes.
        The alternative -- each of `Sha256ContentHasher` and
        `ClipEmbeddingModel` doing its own `if is None` -- is two chances
        for one of them to instead reconstruct a path from
        `relative_path`, which would produce a path relative to the
        process's working directory: a plausible-looking string that reads
        the wrong file or, worse, an unrelated one.

        Raises `DeviceNotConnectedError`, not `FileNotFoundError`, because
        the two mean different things to a caller (see that exception).
        """
        if self.absolute_path is None:
            raise DeviceNotConnectedError(
                f"{self.relative_path} has no resolved location: device "
                f"{self.device_id} is not connected."
            )
        return self.absolute_path

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Image):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
