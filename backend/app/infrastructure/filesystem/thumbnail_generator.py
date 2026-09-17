"""Pillow implementation of the thumbnail port (RFC-030 section 7.2).

**A fresh decode of the file, not a by-product of the embedding.** RFC-030
as proposed expected thumbnails to come almost free, from the picture CLIP
had already decoded. In this codebase that picture never leaves
`ClipEmbeddingModel._preprocess()`, so this module opens the file again --
the fourth independent open per indexed image, after the content hash, the
EXIF header and the model. What that costs is measured in
`experiments/rfc-030-file-access/measure_thumbnail_cost.py`, against the
inference it sits beside rather than against zero.
"""

from __future__ import annotations

import io

from PIL import Image as PILImage
from PIL import ImageOps

from app.domain.entities.image import Image
from app.domain.services.thumbnail_generator_port import ThumbnailGeneratorPort

THUMBNAIL_FORMAT = "JPEG"
THUMBNAIL_SUFFIX = ".jpg"
"""The one encoding every thumbnail has, and the suffix the store names it by.

JPEG because thumbnails of photographs are photographs: it is the smallest
widely decodable format for them, every browser renders it, and the
alternatives that beat it on size (WebP, AVIF) buy bytes the local-first
deployment has no network to save them on. Transparency is flattened onto
white (`_flatten()`), which is the one thing the choice gives up.
"""

JPEG_QUALITY = 85
"""Not configuration, for the reason `READ_CHUNK_BYTES` is not.

Nothing about the product changes between 80 and 90 that a user would
choose; what matters is that it is fixed, so two runs produce the same
bytes. The resulting size per thumbnail is measured in RFC-030 section 9.
"""

_SIXTEEN_BIT_MODES = frozenset({"I;16", "I;16L", "I;16B", "I;16N"})


class PillowThumbnailGenerator(ThumbnailGeneratorPort):
    """Decode, shrink, orient, flatten and encode one image as a JPEG."""

    def generate(self, image: Image, max_edge: int) -> bytes:
        """Return JPEG bytes whose longer side is at most `max_edge` pixels.

        The order of the steps is the performance, and each one is there
        for a case the demo corpus's RFC-022 hard cases actually contain.

        1. **`thumbnail()` before anything forces a full decode.** For a
           JPEG it asks the decoder for a reduced-scale draft (DCT scaling
           to 1/2, 1/4 or 1/8) before resizing, so a 20 MP drone photo is
           never decoded at full resolution. `exif_transpose()` or
           `convert()` first would load every pixel and throw that away.
           It never enlarges a smaller source.
        2. **EXIF orientation is applied to the small image.** A camera
           held vertically writes landscape pixels plus an orientation tag;
           ignoring the tag shows the user a sideways photo. The tag
           survives `thumbnail()`, and a 90-degree turn keeps the result
           inside the `max_edge` square. The output carries no EXIF, so no
           viewer can apply the rotation a second time.
        3. **Everything becomes 8-bit RGB.** CMYK and grayscale convert
           directly; 16-bit TIFFs are scaled down first, because Pillow's
           direct conversion clips every value above 255 and renders a
           16-bit photo almost entirely white; transparency is flattened
           onto white, since JPEG has no alpha and a plain conversion would
           put transparent regions on black.

        A zero-byte or truncated file raises from the decoder, and that is
        the correct outcome: the caller reports a thumbnail failure and
        the image stays indexed without one.
        """
        if max_edge < 1:
            raise ValueError(f"max_edge must be at least 1, got {max_edge}")

        with PILImage.open(image.require_absolute_path().value) as opened:
            opened.thumbnail((max_edge, max_edge))
            oriented = ImageOps.exif_transpose(opened)
            picture = _flatten(_to_eight_bit(oriented))

            buffer = io.BytesIO()
            picture.save(buffer, format=THUMBNAIL_FORMAT, quality=JPEG_QUALITY)

        return buffer.getvalue()


def _to_eight_bit(picture: PILImage.Image) -> PILImage.Image:
    """Scale a 16-bit single-channel image into 8 bits instead of clipping it."""
    if picture.mode not in _SIXTEEN_BIT_MODES:
        return picture
    return picture.convert("I").point(lambda value: value / 257).convert("L")


def _flatten(picture: PILImage.Image) -> PILImage.Image:
    """Return an RGB image, compositing any transparency onto white."""
    has_alpha = picture.mode in ("RGBA", "LA", "PA") or (
        picture.mode == "P" and "transparency" in picture.info
    )
    if not has_alpha:
        return picture.convert("RGB")

    rgba = picture.convert("RGBA")
    background = PILImage.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background
