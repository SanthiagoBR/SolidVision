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
