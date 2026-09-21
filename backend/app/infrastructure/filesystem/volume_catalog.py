"""The `VolumeCatalog` adapter, over the RFC-027 volume provider (RFC-031 section 5).

A thin shell on `VolumeIdentityProvider`, in the shape
`MountedDeviceLocator` already has: the platform knowledge stays in
`WindowsVolumeIdentityProvider` and nothing new here knows what operating
system it is on.

**Nothing caches, and the absence is the design.** Every call enumerates
again, for the reason written into both ports: a mount point is wrong from
the moment the user pulls the cable, and Windows does not tell this
process when they do.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import VolumeIdentity
from app.domain.value_objects.mounted_volume import MountedVolume
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityError,
    VolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


def as_mounted_volume(resolved: ResolvedVolume) -> MountedVolume:
    """Translate the Infrastructure value into the Domain one.

    A module function rather than a method, because the one caller that
    needs it outside this class is `indexing_worker.register_device()`:
    the CLI resolves a volume from a `--root` path, which is a question
    this catalogue does not ask, and then hands the answer to the same
    Application use case the HTTP route uses. One translation, written
    once, so the two entry points cannot disagree about what they are
    registering.
    """
    return MountedVolume(
        identity=resolved.identity,
        mount_point=resolved.mount_point,
        filesystem_label=resolved.filesystem_label,
        total_bytes=resolved.total_bytes,
    )


class MountedVolumeCatalog(VolumeCatalog):
    """Answers from what the operating system reports as mounted, every time."""

    def __init__(self, volume_provider: VolumeIdentityProvider) -> None:
        self._volumes = volume_provider

    def mount_points(self) -> dict[VolumeIdentity, Path]:
        """One enumeration, straight through -- RFC-027 section 7's answer.

        Measured at 0.19 ms median with one volume mounted (RFC-030,
        `experiments/rfc-030-file-access/`), which is what makes it
        affordable on the search path and affordable once per device
        list.
        """
        return self._volumes.mounted_volumes()

    def list_mounted(self) -> list[MountedVolume]:
        """The same enumeration, plus one identification call per volume.

        **This is the expensive method, and it is separate for that
        reason.** `resolve()` reads the filesystem label with
        `GetVolumeInformationW` and the capacity with
        `GetDiskFreeSpaceExW`; the second can spin up an external disk
        that had parked its heads, which is seconds rather than
        milliseconds. `mount_points()` above is on the hot path -- every
        search page resolves its hits through it -- so the detail is paid
        only by the two callers that asked for it, `GET /volumes` and the
        registration that has to write `filesystem_label` and
        `total_bytes` onto a new row (RFC-031 section 4.1 of the build
        prompt).

        A volume that will not answer is skipped and logged, never
        raised on: `mounted_volumes()` already skips the ones that refuse
        to be *named*, and a disk that refuses to be *described* is the
        same situation one question later -- not a reason to stop
        reporting the disks that did answer (RFC-027 section 7).

        The identity kept is the enumeration's, not the one `resolve()`
        re-derives. They are the same string from the same platform call,
        and taking the enumeration's is what guarantees that a key from
        `mount_points()` and an `identity` from here compare equal --
        which is the whole mechanism by which `GET /volumes` knows a
        volume is already a registered device.
        """
        mounted: list[MountedVolume] = []
        for identity, mount_point in self._volumes.mounted_volumes().items():
            try:
                resolved = self._volumes.resolve(mount_point)
            except VolumeIdentityError:
                logger.warning(
                    "Skipping volume at %s: it would not describe itself",
                    mount_point,
                )
                continue
            mounted.append(
                MountedVolume(
                    identity=identity,
                    mount_point=mount_point,
                    filesystem_label=resolved.filesystem_label,
                    total_bytes=resolved.total_bytes,
                )
            )
        return mounted
