"""Where indexed images are right now, asked once per disk (RFC-030 section 4).

Two routes need the same answer -- `GET /images/search` for every hit on a
page, `GET /images/{id}` for one image -- and `POST /images/{id}/reveal`
needs it before it can open anything. None of them may compute it
themselves:

* not the route, because "is this disk plugged in, and where" is exactly
  the kind of rule `AI_Context.md` keeps out of Presentation;
* not `SearchImagesUseCase`, because resolving a location is not searching.
  It decorates a result after the fact, and putting it inside search would
  leave the other two use cases either duplicating it or importing a use
  case to borrow a private method.

So it is its own small use case, composed from the same two ports
`CreateIndexingJobUseCase` already combines: the `DeviceRepository` that
knows each disk's name, and the `DeviceLocator` that knows where it is
mounted this instant.

**Nothing here is persisted and nothing here is cached.** `absolute_path`
lives on the returned `Image` for the length of one request and is never
written (RFC-027 section 7) -- the answer is wrong from the moment the user
pulls the cable, and nothing tells this process when that happens.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath


@dataclass(frozen=True)
class LocatedImage:
    """An image, the disk it is on, and whether that disk is here right now.

    `connected` is derived from `image.absolute_path` rather than stored
    beside it, so the two cannot disagree: an image with a path on an
    unplugged disk, or a plugged-in disk whose image has no path, are not
    states this type can represent.
    """

    image: Image
    """The image with `absolute_path` filled in when its device is mounted."""

    device: Device

    @property
    def connected(self) -> bool:
        """Whether the image's disk was mounted when the location was resolved.

        `False` is an answer, not a failure: "it is on HD3, in 2018/junho"
        is the result the product exists to give for a disk in a drawer
        (RFC-030 section 4.1).
        """
        return self.image.absolute_path is not None


class ResolveImageLocationUseCase:
    """Attach to each image its device, and its absolute path when mounted."""

    def __init__(
        self, device_repository: DeviceRepository, device_locator: DeviceLocator
    ) -> None:
        self._devices = device_repository
        self._locator = device_locator

    def execute(self, images: Sequence[Image]) -> list[LocatedImage]:
        """Return one `LocatedImage` per image, in the order given.

        **The operating system is asked once per distinct device, not once
        per image.** `MountedDeviceLocator.mount_point()` enumerates every
        mounted volume on each call and refuses to cache, on purpose
        (RFC-027 section 7), so a page of ten hits spread over two disks
        must cost two enumerations rather than ten. The grouping lives
        inside this one call and dies with it: remembering an answer
        across calls would be precisely the cache the port forbids.

        The order and the length of `images` are kept exactly, so a caller
        holding a ranked list can pair the two back up without matching
        ids -- the ranking is the repository's and nothing here may
        reorder it.

        An image whose device has no row is a broken invariant rather than
        a request to refuse: `images.device_id` is a foreign key, so
        PostgreSQL cannot produce one. It raises `LookupError`, which no
        error handler dresses up as a 4xx, because a bug must stay a 500.
        """
        located_devices: dict[DeviceId, tuple[Device, Path | None]] = {}
        for device_id in dict.fromkeys(image.device_id for image in images):
            device = self._devices.get(device_id)
            if device is None:
                raise LookupError(
                    f"Device {device_id} has indexed images but no row; "
                    "images.device_id is a foreign key, so this should be "
                    "impossible."
                )
            located_devices[device_id] = (device, self._locator.mount_point(device))

        located: list[LocatedImage] = []
        for image in images:
            device, mount_point = located_devices[image.device_id]
            located.append(
                LocatedImage(image=_with_location(image, mount_point), device=device)
            )
        return located

    def execute_one(self, image: Image) -> LocatedImage:
        """Locate a single image; the one-element case of `execute()`."""
        (located,) = self.execute([image])
        return located


def _with_location(image: Image, mount_point: Path | None) -> Image:
    """Return `image` with its absolute path for `mount_point`, or with none.

    `None` is written explicitly rather than left as whatever the entity
    carried. Rows come back from the repository with no path, but an
    `Image` built by the indexing worker has one, and a resolution that
    kept a stale path for an unplugged disk would report it as connected.
    """
    absolute_path = (
        ImagePath(mount_point / str(image.relative_path))
        if mount_point is not None
        else None
    )
    return dataclasses.replace(image, absolute_path=absolute_path)
