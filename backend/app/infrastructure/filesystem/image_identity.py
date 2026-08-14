"""Deterministic identity derivation for filesystem-discovered images.

The namespace UUID below was generated once via `uuid.uuid4()` and is now
frozen permanently. Regenerating it would silently change the `ImageId`
produced for every already-indexed file, making all of them appear "new".
Never regenerate it, here or in a later RFC, without an explicit and
documented migration strategy.
"""

from __future__ import annotations

import uuid

from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath

SOLIDVISION_PATH_NAMESPACE = uuid.UUID("5dc64f53-522e-4303-951e-ee6123b10dd8")


def compute_image_id(path: ImagePath) -> ImageId:
    """Derive a deterministic `ImageId` from a normalized image path.

    The same path always yields the same id, across runs and machines,
    because `ImagePath.__str__()` already normalizes separators into a
    single posix form before it is hashed.

    Renaming or moving a file changes its path and therefore its id --
    this produces a new image record rather than tracking history across
    renames, which is an accepted limitation for the current scope.
    """
    return ImageId(uuid.uuid5(SOLIDVISION_PATH_NAMESPACE, str(path)))
