"""A volume the operating system reports as attached right now (RFC-031 section 5).

What `ResolvedVolume` is, written down on the Domain's side of the line.
The Application has to answer *"what is plugged into this machine, and
which of it do we already know?"*, and `ResolvedVolume` lives in
`app.infrastructure.filesystem`, which `test_application_architecture.py`
forbids a use case to import.

**The alternative, and why it was refused.** Moving
`VolumeIdentityProvider` and `ResolvedVolume` into the Domain wholesale
would drag `ctypes` and `kernel32` along with them -- the Windows adapter
shares that module with the port -- and splitting the two is a refactor of
RFC-027 inside an RFC about HTTP routes. Restating four fields is cheaper
than that, and the restatement is not duplication for long: the adapter
converts at its own boundary and nothing above it ever sees the
Infrastructure type (RFC-031 section 4.2 of the build prompt).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.value_objects.device_id import VolumeIdentity


@dataclass(frozen=True)
class MountedVolume:
    """One attached volume: its durable name, and where it happens to be.

    Splits in two exactly as `ResolvedVolume` does, and the split is the
    point. `identity` is durable and gets persisted. `mount_point` is not,
    and must not: it is valid until the user unplugs the disk or reboots,
    which is why it travels in a value like this one rather than in a
    column (RFC-027 section 4).
    """

    identity: VolumeIdentity
    mount_point: Path
    """Where the volume is attached *at this instant* -- `D:\\`, typically.

    **Never persist this, and never cache it across a request.** It is a
    function of mount order, and storing it is the defect RFC-027 exists
    to remove. Nothing notifies this process when a cable is pulled, so a
    remembered answer is wrong from that moment and stays wrong.
    """

    filesystem_label: str | None = None
    """Whatever the volume itself reports -- often empty, often `Untitled`.

    Informative, never an identifier. `Device.label` is where the user's
    name for a disk lives, precisely because this one cannot be relied on
    to be either present or distinct.
    """

    total_bytes: int | None = None
    """The volume's capacity, or `None` when the platform will not say.

    The field that makes `VolumeCatalog.list_mounted()` more expensive
    than `mount_points()`: reading it costs a `GetDiskFreeSpaceExW` per
    volume, which can spin up a sleeping external disk (RFC-031 section
    4.1 of the build prompt).
    """
