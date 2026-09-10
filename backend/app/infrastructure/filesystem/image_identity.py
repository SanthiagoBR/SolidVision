"""Deterministic identity derivation for filesystem-discovered images.

The namespace UUID below was generated once via `uuid.uuid4()` and is now
frozen permanently. Regenerating it would silently change the `ImageId`
produced for every already-indexed file, making all of them appear "new".
Never regenerate it, here or in a later RFC, without an explicit and
documented migration strategy.

RFC-027 changed the *string* fed into that namespace and deliberately did
not change the namespace itself. What used to be hashed was the absolute
path, which on Windows contains a drive letter -- and a drive letter is a
function of mount order, not of the disk, so the same untouched file
produced a different id whenever it happened to mount as `F:` instead of
`D:`, duplicating every row and re-running hours of inference (RFC-027
section 2.1). What is hashed now is the device plus the path relative to
that device's mount point, neither of which moves on its own.

Preserving the namespace across that change is intentional. If both the
namespace and the input string changed, an old id could no longer be
recognised as having come from the old scheme.
"""

from __future__ import annotations

import uuid

from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath

SOLIDVISION_PATH_NAMESPACE = uuid.UUID("5dc64f53-522e-4303-951e-ee6123b10dd8")


def compute_image_id(device_id: DeviceId, relative_path: ImagePath) -> ImageId:
    """Derive a deterministic `ImageId` from a device and a path within it.

    The same file on the same disk always yields the same id, across
    runs, across machines, and across drive letters, because neither
    input contains a mount point. `ImagePath.__str__()` normalizes
    separators into one posix form before it is hashed, so a path built
    from a Windows string and one built from a posix string agree.

    Renaming or moving a file *within* a device changes its relative path
    and therefore its id -- this produces a new image record rather than
    tracking history across renames, which is an accepted limitation for
    the current scope. Moving it to another disk changes the id for the
    same reason.
    """
    return ImageId(
        uuid.uuid5(SOLIDVISION_PATH_NAMESPACE, f"{device_id}/{relative_path}")
    )
