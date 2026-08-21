"""Abstract contract for computing a content fingerprint of an image's bytes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities.image import Image


class ContentHasherPort(ABC):
    """Compute a stable fingerprint of the bytes an `Image` points at.

    Mirrors `EmbeddingModelPort`: the Application layer depends on this
    contract, never on the filesystem access an implementation needs to
    satisfy it. Reading a file is I/O, and I/O belongs in Infrastructure.

    The returned fingerprint is a *change-detection* value only. It must
    never be used for identity, deduplication, or row lookup: `ImageId` is
    derived from the path on purpose (RFC-022 7.1), so two byte-identical
    files at two paths are two distinct images that happen to share a
    fingerprint.

    Implementations must treat the supplied `Image` as read-only, and must
    let I/O errors propagate rather than returning a sentinel -- a caller
    that cannot read the bytes has to decide what that means, and only the
    caller knows whether the right answer is "skip this file" or "stop".
    """

    @abstractmethod
    def hash_image(self, image: Image) -> str:
        """Return the fingerprint of the bytes stored at `image.path`."""
