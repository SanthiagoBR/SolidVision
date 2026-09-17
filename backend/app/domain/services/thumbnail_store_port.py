"""Where rendered thumbnails are kept, without naming a directory (RFC-030).

The Application layer decides *that* a thumbnail is written and records
where; it must not open files to do it, any more than it hashes them. This
port is the other half of `ThumbnailGeneratorPort`: that one turns a photo
into bytes, this one keeps the bytes.

**Nothing behind this port may write into a user's collection.** RFC-028
section 10 declared that the system never writes to the collection, and
RFC-030 section 7.1 repeats it for thumbnails with a second reason: a
thumbnail stored beside the photo would vanish into the drawer with the
disk, which is exactly when a user most needs to see what they found.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.domain.value_objects.image_id import ImageId


class ThumbnailStorePort(ABC):
    """Keep a thumbnail per image id, in storage the application owns."""

    @abstractmethod
    def save(self, image_id: ImageId, data: bytes) -> str:
        """Store `data` as the thumbnail of `image_id` and return its location.

        The location is what gets persisted in `images.thumbnail_path`, and
        it is opaque to everyone but the store: a caller hands it back to
        `locate()` and never builds a path from it.

        Saving for an id that already has a thumbnail replaces it in place.
        That is the regeneration RFC-030 section 7.2 describes -- the same
        id, new bytes -- and the reason clients must revalidate rather than
        trust a cached copy for ever.
        """

    @abstractmethod
    def locate(self, location: str) -> Path | None:
        """Return the file behind a stored location, or `None` if it is gone.

        `None` is an honest answer rather than an error: the cache is the
        application's, and a thumbnail that was cleaned away is served as
        "not generated yet" (RFC-030 section 7.2) instead of failing the
        request.
        """
