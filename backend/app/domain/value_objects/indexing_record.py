"""Carrier bundling everything needed to persist an indexed image."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector


@dataclass(frozen=True)
class IndexingRecord:
    """Write-only carrier bundling an image with its embedding and filesystem metadata.

    Exists because `ImageRepository.save()` has no way to convey an
    embedding or filesystem metadata alongside a Domain `Image`. It is
    part of the `ImageRepository` port's own contract -- like `EmbeddingVector`
    or `ImageId` -- so it lives in Domain rather than Application, keeping
    the port self-contained within its own layer. `Image` itself remains
    completely unchanged. Not used as a read type; see `IndexMetadata`
    for that.
    """

    image: Image
    embedding: EmbeddingVector
    file_size: int | None
    file_modified_at: datetime.datetime | None
    content_hash: str | None = None
    """Fingerprint of the bytes that produced `embedding`.

    Optional for the same reason as `IndexMetadata.content_hash`: a writer
    that never computed one persists NULL, and NULL reads back as
    "unknown", which costs a re-embed on the next size/mtime change rather
    than risking a skipped one.
    """

    thumbnail_path: str | None = None
    """Where the thumbnail rendered from these bytes was stored (RFC-030).

    Optional like `content_hash`, and `None` is written as `None`: a
    record reaches the repository because its bytes are new or changed, so
    a thumbnail stored for an *earlier* version of the file no longer
    depicts it. Keeping that stale location would serve the old picture
    under the new `ETag`.

    `None` when rendering failed, which is never a failure of the record
    -- the embedding was paid for and the image is searchable without a
    thumbnail (RFC-030 section 7.2) -- or when the pipeline was composed
    without a thumbnail writer at all.
    """
