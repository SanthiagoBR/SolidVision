"""In-memory device repository for development and dependency wiring (RFC-027)."""

from __future__ import annotations

import uuid
from dataclasses import replace

from app.domain.entities.device import Device
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity


class InMemoryDeviceRepository(DeviceRepository):
    """Repository implementation backed by an in-memory dict.

    Held to the same contract test as `PostgresDeviceRepository`, which is
    the only reason it is worth having: a double that accepted a second
    row for the same volume, or that matched identities loosely, would
    turn every green test above it into evidence about the double.
    """

    def __init__(self) -> None:
        self._devices: dict[uuid.UUID, Device] = {}

    def save(self, device: Device) -> None:
        """Insert or update the device, keyed by id.

        A dict assignment is nearly an upsert for free, which happens to
        be the contract. What it must not become is an append: `DeviceId`
        is derived from the volume identity, so saving the same disk twice
        is the normal case -- every worker run does it -- and a second
        entry would be a duplicate the database's `UNIQUE` constraint
        would have refused.

        `first_seen_at` is carried over from the existing row rather than
        overwritten, matching `PostgresDeviceRepository`. It is the one
        field that answers a question about the past, and a plain
        assignment here would let this double be quietly more forgetful
        than the database it stands in for -- turning "known since March"
        into "known since just now" on every worker run, in the
        implementation that most tests actually exercise.
        """
        existing = self._devices.get(device.id.value)
        if existing is not None:
            device = replace(device, first_seen_at=existing.first_seen_at)
        self._devices[device.id.value] = device

    def get(self, device_id: DeviceId) -> Device | None:
        return self._devices.get(device_id.value)

    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        """Find the device for a volume identity, matching both of its halves.

        `VolumeIdentity` is a frozen dataclass, so `==` already compares
        the opaque string *and* the platform kind. Comparing only the
        string would merge two volumes that two platforms happened to
        name alike.
        """
        for device in self._devices.values():
            if device.volume_identity == identity:
                return device
        return None

    def list(self) -> list[Device]:
        return list(self._devices.values())

    def delete(self, device_id: DeviceId) -> None:
        """Forget the device, and nothing else.

        Deliberately does not touch any image: this repository does not
        know about images, and the port forbids cascading anyway, because
        an image row is an embedding that cost real inference time
        (RFC-027 section 6.3).
        """
        self._devices.pop(device_id.value, None)
