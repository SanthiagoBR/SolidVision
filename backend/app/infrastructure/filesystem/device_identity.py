"""Deterministic identity derivation for storage devices (RFC-027).

The counterpart of `image_identity.py`, and it works the same way and for
the same reason: a `DeviceId` is `uuid5` over something the platform
reports, so plugging the same disk in on a second machine, or after a
reboot, or into a different port, lands on the same row without anything
having to be looked up first.

The namespace UUID below was generated once via `uuid.uuid4()` and is now
frozen permanently. Regenerating it would mint a second `DeviceId` for
every disk already known, orphaning every image row that points at the
first one. Never regenerate it.
"""

from __future__ import annotations

import uuid

from app.domain.value_objects.device_id import DeviceId, VolumeIdentity

SOLIDVISION_VOLUME_NAMESPACE = uuid.UUID("6a1f4c07-9f0c-4a2e-9d84-0f3f0d5c4b91")


def compute_device_id(volume_identity: VolumeIdentity) -> DeviceId:
    """Derive a deterministic `DeviceId` from a volume's stable identity.

    Hashes `str(volume_identity)`, which is `kind:value` -- both halves,
    so that two platforms reporting the same opaque string still describe
    two different devices. That is the same reason `VolumeKind` exists at
    all: the discriminator has to be part of the key, not a comment
    beside it.

    Reformatting the volume changes what the platform reports and
    therefore changes the id. Accepted without special handling: a format
    destroys the photos, so orphaning their device is the correct answer
    rather than a defect (RFC-027 section 4.1).
    """
    return DeviceId(uuid.uuid5(SOLIDVISION_VOLUME_NAMESPACE, str(volume_identity)))
