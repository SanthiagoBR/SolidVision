"""Device entity for the domain layer (RFC-027).

A device is one physical volume: an external disk, a memory card, the
internal drive. It is *not* a `Collection` -- one disk holds several
indexed folders, indexed at different times against different model
versions, and fusing the two would force a whole disk to share one
embedding model version (RFC-027 section 10).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.value_objects.device_id import DeviceId, VolumeIdentity


@dataclass(frozen=True)
class Device:
    """Immutable business entity representing a storage volume.

    Frozen and comparing by `id`, following `Image` (RFC-009): two values
    describing the same disk at two moments -- a different `last_seen_at`,
    a renamed label -- are the same device, and a set of devices must not
    contain it twice.

    **There is no `mount_point` field, and no `drive_letter` field.**
    Persisting either would reintroduce precisely the unstable datum this
    entity exists to eliminate: on Windows a removable volume's letter is
    a function of mount order, so the same disk is `D:` today and `F:`
    tomorrow while nothing on it changed (RFC-027 section 2.1). Where the
    volume is mounted *right now* is resolved when it is asked for, by
    `VolumeIdentityProvider.mounted_volumes()`, and is never written down.

    For the same reason there is no `is_connected` field. It would be
    wrong on every read taken after the user unplugged the disk, and
    nothing would ever correct it -- Windows does not notify this process
    (RFC-027 section 7).
    """

    id: DeviceId
    volume_identity: VolumeIdentity
    label: str
    """The user's name for the disk -- "HD2".

    Separate from `filesystem_label` because that one is frequently
    `Untitled`, or empty, or the vendor's marketing string. A product that
    promises to say *"it is on HD3"* needs somewhere to keep "HD3".
    """

    filesystem_label: str | None = None
    """Whatever the volume itself reports. Informative, never an identifier."""

    total_bytes: int | None = None
    first_seen_at: datetime.datetime | None = None
    last_seen_at: datetime.datetime | None = None
    """When this disk was last observed plugged in -- history, not state.

    The distinction is the one RFC-027 section 7 makes: this answers
    "when did I last see it", which is a fact about the past and stays
    true. It never answers "is it plugged in now", which is a fact about
    the present that no stored value can keep.
    """

    last_scan_at: datetime.datetime | None = None
    last_scan_file_count: int | None = None
    """How many supported files the last scan found on this volume.

    The denominator of the "% indexed" figure, persisted separately from
    the indexing result because the two answer different questions
    (RFC-027 section 8). A scan is cheap -- `stat`, no inference -- and
    without its own count the percentage could only ever be computed
    against rows already in `images`, which reaches 100% after any
    complete pipeline run and never sees a file that was never scanned.

    Anything rendering a percentage from this must also render *when* the
    scan happened. "50%" alone is a confident wrong number for a user who
    copied 10,000 photos onto the disk yesterday.
    """

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Device):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
