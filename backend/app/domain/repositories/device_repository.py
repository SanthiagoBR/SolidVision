"""Abstract repository port for device persistence operations (RFC-027)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity


class DeviceRepository(ABC):
    """Repository contract for managing `Device` domain entities.

    Sits beside `ImageRepository` rather than inside it: a device outlives
    every image on it, is created by a different act (plugging a disk in),
    and is looked up by a key -- the volume identity -- that no image
    carries.

    Nothing here reports whether a device is connected. That question has
    no persistent answer (RFC-027 section 7); it is asked of
    `VolumeIdentityProvider`, which enumerates what is mounted *now*, and
    an implementation of this port that grew an `is_connected` column
    would be answering it from a value that stopped being true the moment
    the user unplugged the disk.
    """

    @abstractmethod
    def save(self, device: Device) -> None:
        """Create the device, or update the row that already has its id.

        An upsert rather than a create-once, because the natural call site
        is a worker starting a run against a disk it may or may not have
        seen before, and every field except the identity is expected to
        move between runs: `last_seen_at` always, `label` when the user
        renames it, `total_bytes` when the volume grows, the scan counters
        after every scan.

        `id` is derived from `volume_identity`, so saving the same volume
        twice updates one row rather than creating a second. An
        implementation must therefore key on `id` and let the `UNIQUE`
        constraint on the identity stay a backstop, not the mechanism.
        """

    @abstractmethod
    def get(self, device_id: DeviceId) -> Device | None:
        """Retrieve a device by its identifier, or `None` when unknown."""

    @abstractmethod
    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        """Retrieve the device for a volume the operating system just reported.

        The lookup that makes remounting free. A worker resolves the
        identity of whatever it was pointed at, asks this, and either
        finds the disk it indexed last month -- under a different drive
        letter, and it does not matter -- or learns that this is a new one.

        Matching is exact on both halves of the identity, string and kind.
        No case folding, no normalisation of separators: the value is
        opaque above the adapter that minted it, and an implementation
        that "helpfully" folded case would merge two volumes on a platform
        whose identifiers are case-sensitive.
        """

    @abstractmethod
    def list(self) -> list[Device]:
        """Return every device known to the repository.

        Order is not part of the contract. Callers that render a device
        list -- a sidebar, say -- sort by whatever they display.
        """

    @abstractmethod
    def delete(self, device_id: DeviceId) -> None:
        """Remove a device from the repository.

        Does nothing when no such device exists. Implementations must not
        cascade into `images`: an image row is an embedding that cost real
        inference time, and RFC-027 section 6.3 is explicit that rows for
        an absent disk are a record of something to plug in, never
        something to delete quietly. A repository backed by a database
        with a foreign key will therefore refuse the delete while images
        remain, and that refusal is the correct behaviour.
        """
