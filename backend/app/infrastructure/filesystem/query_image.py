"""A picture sent as a search query, decoded before the model sees it (RFC-038 4.2).

`POST /images/search/by-image` writes the bytes it received to a temporary
file and hands the path to CLIP. The adapter decodes it again in
`_preprocess`, and lets the decoder's errors through on purpose -- for an
indexed file they are a fault worth a traceback. For a file the user just
dropped they are the user's, and a JPEG cut short must be a 400 that says so,
not a 500.

So this opens the file first, with the same Pillow, and refuses what the
adapter would choke on: bytes that are no image, a format other than the one
declared, more pixels than `Image.MAX_IMAGE_PIXELS` -- Pillow's default
guard against decompression bombs, 89,478,485 -- and a file whose pixels do
not decode to the end. Decoding twice, here and in the adapter, is the price,
and it is in the measured time of RFC-038 section 8.

Infrastructure, and not the dependency that calls it: `PIL` is kept out of
`app/presentation/api/` (`tests/test_ai_layer_boundaries.py`).
"""

from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.domain.exceptions import UnreadableQueryImageError

PILLOW_FORMATS = {"image/jpeg": ("JPEG", "MPO"), "image/png": ("PNG",)}
"""The media types a query picture may have, and what Pillow calls each.

**A DJI photo is `MPO` to Pillow**, not `JPEG`: a JPEG with a second picture,
the preview, after the first (CIPA's Multi-Picture Format). Every one of the
40 real DJI files of RFC-032 opens as `MPO`, and the first version of this
check refused them all -- the photos the product exists to find -- with a 400.
The measurement of RFC-038 found it; the first frame is the photo, and it is
what the adapter reads."""


def inspect_query_image(path: Path, media_type: str) -> None:
    """Decode the picture at `path`, or raise `UnreadableQueryImageError`.

    `media_type` is the one the request declared, already checked against the
    first bytes; the format Pillow identifies must agree with it. The pixel
    limit is checked against the size in the header, before anything is
    decoded, so a bomb costs a header read. `MAX_IMAGE_PIXELS` is read at
    every call, as Pillow itself reads it.
    """
    expected = PILLOW_FORMATS.get(media_type)
    if expected is None:
        raise UnreadableQueryImageError(f"{media_type} is not a query image type.")
    try:
        with warnings.catch_warnings():
            # Above the limit Pillow only warns, up to twice it; the check
            # below refuses at the limit itself.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as picture:
                if picture.format not in expected:
                    raise UnreadableQueryImageError(
                        f"The picture is {picture.format}, but was sent as "
                        f"{media_type}."
                    )
                width, height = picture.size
                limit = Image.MAX_IMAGE_PIXELS
                if limit is not None and width * height > limit:
                    raise UnreadableQueryImageError(
                        f"The picture has {width} x {height} pixels, more than "
                        f"the {limit} this decoder agrees to open."
                    )
                picture.load()
    except UnreadableQueryImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise UnreadableQueryImageError(
            f"The picture cannot be read: {error}"
        ) from error
    except (OSError, SyntaxError, ValueError) as error:
        # A truncated JPEG raises `OSError` from `load()`; a corrupt PNG chunk,
        # `SyntaxError` or `ValueError`, depending on where it breaks.
        raise UnreadableQueryImageError(
            f"The picture cannot be read: {error}"
        ) from error
