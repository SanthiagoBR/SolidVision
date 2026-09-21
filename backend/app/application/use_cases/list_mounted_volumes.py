"""What is plugged in, and what of it we already know (RFC-031 §5).

The question that comes *before* registering a disk, and the reason
`POST /api/v1/devices` never has to accept a path: the server enumerates
the volumes and mints an opaque identifier for each one, and the client
hands one of those back. The client never composes a location, and an
identifier it invented resolves to nothing (RFC-031 section 5.1).

`device_id` on each row is what lets the "add a device" screen show the
disks it already knows as already-added, instead of letting a user
register one twice and discover afterwards that nothing happened.

**This route is deliberately the expensive one.** RFC-031 section 5 calls
it *"a direct projection of `mounted_volumes()`"*; it is not.
`mounted_volumes()` returns identities and mount points and knows
neither the filesystem label nor the capacity, both of which are in the
response body -- so this goes through `VolumeCatalog.list_mounted()`,
which pays one identification call per volume. Section 4.1 of the build
prompt records why that cost was given its own method rather than folded
into the cheap one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.repositories.device_repository import DeviceRepository
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.mounted_volume import MountedVolume


@dataclass(frozen=True)
class CatalogedVolume:
    """One attached volume, and the device it already is -- or `None`."""

    volume: MountedVolume

    device_id: DeviceId | None
    """The registered device for this volume, or `None` if it is new here.

    Not derived by hashing the identity, although the derivation is
    deterministic and would give the same answer. `compute_device_id()`
    says what a device *would* be called; this field says whether a row
    exists, which is the question the screen is asking.
    """

    @property
    def is_registered(self) -> bool:
        return self.device_id is not None


class ListMountedVolumesUseCase:
    """Enumerate the attached volumes and mark the ones already registered."""

    def __init__(
        self, volume_catalog: VolumeCatalog, device_repository: DeviceRepository
    ) -> None:
        self._catalog = volume_catalog
        self._devices = device_repository

    def execute(self) -> list[CatalogedVolume]:
        """Return every mounted volume, sorted by where it is attached.

        One enumeration and one `SELECT`, crossed in memory on the volume
        identity -- which is a key, compared for equality and nothing
        else. No case folding and no normalisation: the value is opaque
        above the adapter that minted it, and a repository that folded
        case would merge two volumes on a platform whose identifiers are
        case-sensitive (`DeviceRepository.get_by_volume_identity`).

        Volumes the platform refuses to describe never reach here; the
        adapter skips them, exactly as RFC-027 decided a disk that will
        not answer is no reason to stop reporting the ones that did.

        Sorted by mount point, because that is how the user sees them --
        `D:`, `F:`, `G:` -- and because `list_mounted()` promises no
        order. Sorting here rather than in the adapter keeps the choice
        in the layer that knows it is being rendered.
        """
        known = {device.volume_identity: device.id for device in self._devices.list()}
        cataloged = [
            CatalogedVolume(volume=volume, device_id=known.get(volume.identity))
            for volume in self._catalog.list_mounted()
        ]
        return sorted(cataloged, key=lambda entry: str(entry.volume.mount_point))
