"""Serving a thumbnail, and telling a client its copy is current (RFC-030 section 7.2).

The version of a thumbnail is the `content_hash` of the photo it was
rendered from. RFC-024 already stores that hash for the incremental
decision, so it costs nothing new, and it changes exactly when the thumbnail
does: a photo overwritten in place keeps its id -- the id is derived from the
*path* (RFC-027) -- and gets a new hash, a new embedding and a new thumbnail
in the same reprocessing.

`content_hash` stays off the `Image` entity. It is a change signal, like
`file_size`, and is read here through `IndexMetadata` exactly as the skip
decision reads it.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from app.domain.exceptions import ImageNotFoundError, ThumbnailNotFoundError
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.value_objects.image_id import ImageId


@dataclass(frozen=True)
class ThumbnailFile:
    """The thumbnail to send, and the version to label it with."""

    path: Path
    version: str | None
    """The photo's content hash, or `None` for a row indexed before RFC-024.

    `None` means the thumbnail has no version to validate against, and
    the caller must not invent one -- a validator that never matches is a
    cache that silently never works.
    """


@dataclass(frozen=True)
class ThumbnailUnchanged:
    """The caller already holds this version; nothing needs to be sent."""

    version: str


class GetThumbnailUseCase:
    """Find an image's thumbnail, or confirm the caller's copy is still right."""

    def __init__(
        self, repository: ImageRepository, thumbnail_store: ThumbnailStorePort
    ) -> None:
        self._repository = repository
        self._store = thumbnail_store

    def execute(
        self, image_id: ImageId, held_versions: Collection[str] = ()
    ) -> ThumbnailFile | ThumbnailUnchanged:
        """Return the file to send, or say the caller's version is current.

        `held_versions` are the versions the caller says it already has --
        the tags of an HTTP `If-None-Match`, with the quoting already
        removed. Matching them is not HTTP-specific, which is why it is here
        rather than in the route: "only give me the picture if mine is out
        of date" is a question any client could ask.

        **The store is not consulted when the answer is "unchanged".** The
        decision needs one metadata read and nothing else, so revalidating a
        page of fifty thumbnails costs fifty scalar lookups and zero file
        opens. That also means a client holding the current version is told
        so even if the cached file was cleaned away since -- which is right:
        its copy is still the correct picture.

        Raises `ImageNotFoundError` for an id with no row, and
        `ThumbnailNotFoundError` -- also a 404 -- when the row has no
        thumbnail or the cache no longer holds the file. The UI shows a
        placeholder for both (RFC-030 section 7.2).

        The device the photo lives on is never asked about. The thumbnail
        is in the application's own cache, which is the point of keeping it
        there: it is servable while the disk is in a drawer (RFC-030
        section 4.1).
        """
        metadata = self._repository.get_index_metadata(image_id)
        if metadata is None:
            raise ImageNotFoundError(f"No indexed image with id {image_id.value}.")
        if metadata.thumbnail_path is None:
            raise ThumbnailNotFoundError(
                f"Image {image_id.value} has no thumbnail yet."
            )

        version = metadata.content_hash
        if version is not None and version in held_versions:
            return ThumbnailUnchanged(version=version)

        path = self._store.locate(metadata.thumbnail_path)
        if path is None:
            raise ThumbnailNotFoundError(
                f"The thumbnail of image {image_id.value} is no longer in the cache."
            )
        return ThumbnailFile(path=path, version=version)
