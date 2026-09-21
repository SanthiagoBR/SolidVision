"""Asking the machine what is plugged into it, without importing the platform.

The question `DeviceLocator` cannot answer. That port takes a `Device` in
every signature, because it answers *"where is this disk I already know
about"*. `GET /api/v1/volumes` asks about the **machine** -- about volumes
that are not a device yet and may never become one -- so there is no
`Device` to pass, and a fourth method on `DeviceLocator` would have given
`FakeDeviceLocator` two identities (RFC-031 section 4.2 of the build
prompt).

**Nothing may cache across calls**, for the reason written into
`DeviceLocator`: a mount point is a fact about this instant, the user
pulls the cable, and nothing notifies this process. The provider in
`app/presentation/dependencies/` therefore builds a catalogue per
request, and must never grow an `lru_cache`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.domain.value_objects.device_id import VolumeIdentity
from app.domain.value_objects.mounted_volume import MountedVolume


class VolumeCatalog(ABC):
    """Port for enumerating the volumes attached to this machine right now."""

    @abstractmethod
    def mount_points(self) -> dict[VolumeIdentity, Path]:
        """Return every volume mounted right now, mapped to where it is.

        The cheap half, and the one on the hot path. One enumeration
        answers "is this disk connected, and where" for **every** device
        at once: a caller with twenty devices resolves the whole list
        against this one mapping rather than asking twenty times
        (RFC-031 section 4.1).

        Callers may hold the result for the length of one request and
        must not hold it longer.
        """

    @abstractmethod
    def list_mounted(self) -> list[MountedVolume]:
        """The same volumes, each with its filesystem label and capacity.

        **Deliberately a second method rather than a richer first one.**
        The label and the capacity cost an extra platform call per volume,
        and reading a capacity can spin up an external disk that was
        asleep -- seconds, not milliseconds. `mount_points()` is called on
        every search page through `ResolveImageLocationUseCase`, so
        folding the detail into it would put that cost in front of every
        query in the product in order to serve one screen that is opened
        rarely (RFC-031 section 4.1 of the build prompt).

        Volumes the platform refuses to name are skipped rather than
        raised on, exactly as `mounted_volumes()` skips them: an empty
        optical drive and a locked BitLocker volume are not reasons to
        stop reporting the disks that did answer (RFC-027 section 7).

        Order is not part of the contract. Callers that render the list
        sort by whatever they display.
        """
