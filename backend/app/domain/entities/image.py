"""Image entity for the domain layer."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.exceptions import DeviceNotConnectedError
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
