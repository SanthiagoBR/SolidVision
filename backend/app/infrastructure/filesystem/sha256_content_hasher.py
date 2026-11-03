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
        """Return the hex-encoded SHA-256 digest of the image's bytes.

        Reads `Image.require_absolute_path()` rather than joining
        `relative_path` onto anything: since RFC-027 an image records
        where it lives on its *device*, and only whoever resolved that
        device's mount point knows where that is right now. An image whose
        device is unplugged raises `DeviceNotConnectedError` here, which
        is the truthful answer -- there are no bytes to hash.
        """
        digest = hashlib.sha256()

        with image.require_absolute_path().value.open("rb") as stream:
            while chunk := stream.read(READ_CHUNK_BYTES):
                digest.update(chunk)

        return digest.hexdigest()


def sha256_of(data: bytes) -> str:
    """The fingerprint `Sha256ContentHasher` would give a file holding `data`.

    For the read-ahead of RFC-045, which reads a whole file at once and
    hashes what it read instead of opening the file a second time. Here,
    beside the streaming version, so that the two cannot drift apart in
    algorithm or in spelling -- lowercase hex, as the column stores it; a
    test holds them to the same digest for the same files.
    """
    return hashlib.sha256(data).hexdigest()
