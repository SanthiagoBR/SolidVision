"""SHA-256 implementation of the content-hashing port (RFC-024 section 4)."""

from __future__ import annotations

import hashlib

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort

READ_CHUNK_BYTES = 1024 * 1024
"""How much of a file is held in memory at once while hashing.

Not configuration, so it is not in `Settings`: nothing about the product
changes if this is 512 KiB or 4 MiB. What matters is that it is *bounded*.
`Path.read_bytes()` would be shorter and would work perfectly on the 1.8 MB
demo corpus, then allocate the whole file for a 200 MB TIFF -- and this runs
immediately before batch inference, which is already the memory-hungry part
of the pipeline (RFC-024 section 9).
"""


class Sha256ContentHasher(ContentHasherPort):
    """Fingerprint an image's bytes with SHA-256, streaming the file.

    Lives in `infrastructure/filesystem/` rather than beside the use case
    because hashing a file is filesystem I/O; the Application layer depends
    on `ContentHasherPort` and never learns that a real disk was involved.

    Errors propagate untouched, matching `ClipEmbeddingModel`: a missing or
    unreadable file is the caller's decision to make, and the callers
    already isolate failures per file.
    """

    def hash_image(self, image: Image) -> str:
        """Return the hex-encoded SHA-256 digest of the bytes at `image.path`."""
        digest = hashlib.sha256()

        with image.path.value.open("rb") as stream:
            while chunk := stream.read(READ_CHUNK_BYTES):
                digest.update(chunk)

        return digest.hexdigest()
