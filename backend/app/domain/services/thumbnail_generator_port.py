"""Abstract contract for rendering a thumbnail of an image's bytes (RFC-030)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.entities.image import Image


class ThumbnailGeneratorPort(ABC):
    """Render a small, displayable copy of the picture an `Image` points at.

    Shaped like `ContentHasherPort`, and for the same reason: decoding a
    photograph is I/O plus an imaging library, both of which belong in
    Infrastructure, while *deciding when a thumbnail is worth rendering*
    belongs to the pipeline that calls this.

    **Every call decodes the file afresh.** RFC-030 section 7.2 as proposed
    said a thumbnail would come "from the image already decoded" for CLIP,
    at the cost of a resize and an encode. It cannot, in this codebase: the
    decoded picture is a local of `ClipEmbeddingModel._preprocess()`, which
    sits behind `EmbeddingModelPort` and returns only a fixed-size tensor.
    Handing the decoded picture back through that port was considered and
    refused -- it would give the embedding adapter a responsibility that
    has nothing to do with embeddings, and make every embedding double
    learn to draw thumbnails. So this is a fourth, independent open of the
    file, beside the hash, the EXIF header read and the model's decode,
    each for its own narrow purpose.

    Implementations read `Image.require_absolute_path()`, which raises
    `DeviceNotConnectedError` for an unmounted device, and let decode and
    I/O errors propagate rather than returning a placeholder -- the caller
    decides whether a failed thumbnail is worth reporting, and RFC-030
    decided it never fails the indexing of the image.
    """

    @abstractmethod
    def generate(self, image: Image, max_edge: int) -> bytes:
        """Return encoded thumbnail bytes for `image`, decoding it fresh.

        The longer side of the result is at most `max_edge` pixels and the
        aspect ratio is preserved. A source smaller than that is never
        enlarged: an upscaled thumbnail costs bytes and shows nothing the
        original did not.
        """
