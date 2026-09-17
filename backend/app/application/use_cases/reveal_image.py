"""Show an indexed photo in the file manager (RFC-030 section 5).

**The only input is an id.** The path this opens is built here, from the row
the id names and the mount point the device has right now; no route accepts
a path in a body, a query or a header. That is what makes directory
traversal, `..`, and a UNC path to another machine impossible rather than
filtered: there is no client-supplied string to sanitise (RFC-030 section
5.1).

The two guards of RFC-030 section 6 -- the operator's setting and the
loopback check -- are not here. Both are facts about the HTTP request and
the deployment, and the route enforces them before this use case is ever
constructed.
"""

from __future__ import annotations

from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.domain.exceptions import (
    DeviceNotConnectedError,
    FileGoneError,
    ImageNotFoundError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.value_objects.image_id import ImageId


class RevealImageUseCase:
    """Open the platform's file manager with one indexed image selected."""

    def __init__(
        self,
        repository: ImageRepository,
        location_resolver: ResolveImageLocationUseCase,
        file_revealer: FileRevealerPort,
    ) -> None:
        self._repository = repository
        self._locations = location_resolver
        self._revealer = file_revealer

    def execute(self, image_id: ImageId) -> None:
        """Reveal the image, or say precisely why it cannot be revealed.

        Three refusals, each a different thing for the user to do:

        * no such image -- `ImageNotFoundError`, 404;
        * its disk is not plugged in -- `DeviceNotConnectedError`, 409,
          raised by `Image.require_absolute_path()` rather than by a second
          check written here. The message names the disk to fetch;
        * the disk is plugged in and the file is not on it -- `FileGoneError`,
          410. The photo was deleted, moved or renamed after indexing, and
          fetching a disk would not help.

        The missing-file check happens in the adapter, which raises
        `FileNotFoundError` like any I/O would, and is translated to the
        domain error here: reading the disk is Infrastructure's job, and
        deciding that a missing file is a 410 rather than a 404 is not.
        """
        image = self._repository.get(image_id)
        if image is None:
            raise ImageNotFoundError(f"No indexed image with id {image_id.value}.")

        located = self._locations.execute_one(image)
        try:
            path = located.image.require_absolute_path()
        except DeviceNotConnectedError as error:
            # The same error, re-raised only to name the disk by the label
            # the user gave it: the entity knows the device's id, and "plug
            # in 564e9201-..." is not advice anyone can follow.
            raise DeviceNotConnectedError(
                f"{image.relative_path} is on {located.device.label!r}, which is "
                "not connected. Plug it in and try again."
            ) from error

        try:
            self._revealer.reveal(path)
        except FileNotFoundError as error:
            raise FileGoneError(
                f"{image.relative_path} is no longer on {located.device.label!r}. "
                "It was moved, renamed or deleted after it was indexed."
            ) from error
