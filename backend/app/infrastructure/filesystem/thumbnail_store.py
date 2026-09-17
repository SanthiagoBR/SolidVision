"""Thumbnails on local disk, in a directory the application owns (RFC-030 section 7.1).

The directory is `settings.thumbnail_directory`, and the one rule that
matters about it is where it is *not*: never inside a user's collection.
RFC-028 section 10 says the system never writes to the collection, and
RFC-030 section 7.1 adds the reason that is specific to thumbnails -- kept
beside the photos, they would go into the drawer with the disk, which is
exactly when they are the only way to see what a search found.

The indexing scan skips this directory even when it lies inside a scanned
folder (`FilesystemImageProvider(excluded_directories=...)`). Without that,
indexing a whole system disk would discover the thumbnails as photographs,
render thumbnails of them, and discover those on the next run.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.value_objects.image_id import ImageId
from app.infrastructure.filesystem.thumbnail_generator import THUMBNAIL_SUFFIX


class FilesystemThumbnailStore(ThumbnailStorePort):
    """One JPEG per image id, sharded by the id's first two hex digits."""

    def __init__(self, directory: Path) -> None:
        """Keep thumbnails under `directory`, which is created on first write.

        Resolved once here, so that a relative setting means the same
        directory for every call, and so that `locate()` has a fixed root
        to check a location against.
        """
        self._directory = directory.resolve()

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, image_id: ImageId, data: bytes) -> str:
        """Write `data` for `image_id` atomically and return its location.

        **The location is relative to the store's directory**, so moving
        the cache means moving a folder rather than rewriting every row --
        the argument RFC-027 made for `relative_path`.

        **Sharded by the first two hex digits of the id.** A hundred
        thousand files in one directory work, but make that directory
        unusable in Explorer and slow to back up; 256 subdirectories of a
        few hundred files each cost nothing to address, because the shard
        is derived from the id rather than looked up.

        **Written to a temporary name, then renamed over the target.**
        The same id is overwritten whenever its bytes change (RFC-030
        section 7.2), and a request served while a plain write was in
        progress would stream half a JPEG. `os.replace` is atomic on the
        same volume. The temporary name does not end in `.jpg`, so even a
        scan that did walk this directory would not take it for a photo.
        If the replace fails -- on Windows, because a response is streaming
        the old file at that instant -- the error propagates and the
        caller records a thumbnail failure; the temporary file is removed.
        """
        hex_id = image_id.value.hex
        location = f"{hex_id[:2]}/{image_id.value}{THUMBNAIL_SUFFIX}"
        target = self._directory / location
        target.parent.mkdir(parents=True, exist_ok=True)

        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return location

    def locate(self, location: str) -> Path | None:
        """Return the stored file for `location`, or `None` if it is not there.

        The location came from this application's own database, not from a
        client, and is still checked against the directory: a row edited by
        hand to `../../somewhere` must not turn the thumbnail endpoint into
        a way to read an arbitrary file. The check is on the resolved path,
        so a link inside the cache pointing out of it is refused too.
        """
        candidate = (self._directory / location).resolve()
        if not candidate.is_relative_to(self._directory):
            return None
        if not candidate.is_file():
            return None
        return candidate
