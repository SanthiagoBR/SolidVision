"""Deterministic identity derivation for storage devices (RFC-027).

A `DeviceId` is `uuid5` over something the platform reports, so plugging
the same disk in on a second machine, or after a reboot, or into a
different port, lands on the same row without anything having to be
looked up first.

**This lives in the Domain since RFC-031, and it used to live in
`app/infrastructure/filesystem/device_identity.py`.** Nothing about the
derivation was ever platform-specific: it hashes a `VolumeIdentity`,
which is a Domain value object, and produces a `DeviceId`, which is
another one. It sat in Infrastructure only because its one caller did.
RFC-031 gave it a second caller in the Application layer --
`RegisterDeviceUseCase`, which mints the id for a disk registered over
HTTP -- and `test_application_architecture.py` forbids a use case to
import `app.infrastructure`, so the function moved to the layer it
always belonged to rather than the rule being bent around it.

**The namespace UUID came with it, byte for byte.** It was generated once
via `uuid.uuid4()` and is frozen permanently: regenerating it would mint a
second `DeviceId` for every disk already known, orphaning every image row
that points at the first one -- `ImageId` is `uuid5` over
`f"{device_id}/{relative_path}"`, so it is half of every image identity in
the system. Never regenerate it.

`compute_image_id()` deliberately did **not** move. It is not needed above
Infrastructure, and a gratuitous refactor "for symmetry" would be a
refactor of the identity of every image in the database.
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
