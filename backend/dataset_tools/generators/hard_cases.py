"""Generate the structural edge-case corpus described in RFC-022 section 6.2.

These files are generated rather than committed (RFC-022 open question 1,
now resolved): every case here can be synthesized faithfully with Pillow,
and generating them avoids committing deliberately-corrupt binaries and
avoids depending on how a given filesystem or git client normalizes an
accented filename.

Expectations travel with the generator rather than in a separate manifest
file, so a case and its asserted behavior cannot drift apart.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# Indexing outcomes asserted on a FIRST run against an empty database.
#
# `INDEXED`  -- discovered, and a row is written.
# `IGNORED`  -- never discovered; filtered out by the extension check in
#               `FilesystemImageProvider.discover()`, so it never reaches
#               the use case at all.
# `FAILED`   -- discovered, but indexing raised and was caught by the
#               per-file `except` in `IndexingWorker.run()`.
#
# The incremental "unchanged, so skipped" outcome is deliberately not an
# expectation here: it is a property of a SECOND run and applies uniformly
# to every file that indexed on the first, so it is asserted once by the
# two-run test rather than per case.
INDEXED = "indexed"
IGNORED = "ignored"
FAILED = "failed"

IMAGE_SIZE = (64, 48)


@dataclass(frozen=True)
class HardCase:
    """A single structural edge case and the behavior it pins down."""

    relative_path: str
    expect: str
    description: str
    expect_with_pixel_decoding: str | None = None
    """Outcome once something in the pipeline actually opens the file.

    `None` means the outcome does not change. A value means today's
    `expect` is a consequence of nothing reading pixels yet -- see the
    module docstring of `dataset_tools.generators` and RFC-022 section 7.3.
    `FakeEmbeddingModel.encode_image()` hashes the id and path only, so a
    file whose *bytes* are broken still indexes cleanly right now.
    """


HARD_CASES: tuple[HardCase, ...] = (
    HardCase(
        relative_path="cmyk.jpg",
        expect=INDEXED,
        description="CMYK colorspace JPEG rather than RGB",
    ),
    HardCase(
        relative_path="grayscale.jpg",
        expect=INDEXED,
        description="Single-channel grayscale JPEG",
    ),
    HardCase(
        relative_path="sixteen_bit.tiff",
        expect=INDEXED,
        description="16-bit-per-channel TIFF",
    ),
    HardCase(
        relative_path="alpha.png",
        expect=INDEXED,
        description="RGBA PNG with a partially transparent region",
    ),
    HardCase(
        relative_path="exif_rotated.jpg",
        expect=INDEXED,
        description="JPEG carrying EXIF orientation 6 (rotate 90 CW)",
    ),
    HardCase(
        relative_path="uppercase_extension.JPG",
        expect=INDEXED,
        description="Uppercase .JPG suffix; extension must persist lowercased",
    ),
    HardCase(
        relative_path="fazenda São João.jpg",
        expect=INDEXED,
        description="Filename with spaces and non-ASCII accented characters",
    ),
    HardCase(
        relative_path="nested/level_a/level_b/level_c/deep.jpg",
        expect=INDEXED,
        description="Deeply nested subdirectory, reached only via rglob",
    ),
    HardCase(
        relative_path="duplicate_content_a.jpg",
        expect=INDEXED,
        description="Byte-identical twin of duplicate_content_b.jpg",
    ),
    HardCase(
        relative_path="duplicate_content_b.jpg",
        expect=INDEXED,
        description=(
            "Byte-identical twin of duplicate_content_a.jpg; the pair must "
            "produce TWO rows, pinning the path-derived id in RFC-022 7.1"
        ),
    ),
    HardCase(
        relative_path="zero_byte.jpg",
        expect=INDEXED,
        expect_with_pixel_decoding=FAILED,
        description="Empty file with a supported extension",
    ),
    HardCase(
        relative_path="truncated.jpg",
        expect=INDEXED,
        expect_with_pixel_decoding=FAILED,
        description="Valid JPEG header followed by a hard cut mid-scan",
    ),
    HardCase(
        relative_path="unsupported.gif",
        expect=IGNORED,
        description="GIF is absent from SUPPORTED_IMAGE_EXTENSIONS",
    ),
    HardCase(
        relative_path="unsupported.txt",
        expect=IGNORED,
        description="Plain text file, never a candidate for indexing",
    ),
)


def _gradient(mode: str) -> Image.Image:
    """Build a deterministic image so regenerating yields identical bytes."""
    width, height = IMAGE_SIZE
    image = Image.new(mode, IMAGE_SIZE)
    pixels = image.load()
    if pixels is None:  # pragma: no cover - Pillow always provides an accessor
        raise RuntimeError("Pillow returned no pixel accessor")

    for y in range(height):
        for x in range(width):
            level = (x * 4 + y * 2) % 256
            if mode == "RGB":
                pixels[x, y] = (level, (level + 80) % 256, (level + 160) % 256)
            elif mode == "RGBA":
                pixels[x, y] = (level, 255 - level, (level + 40) % 256, level)
            elif mode == "CMYK":
                pixels[x, y] = (level, (level + 60) % 256, (level + 120) % 256, 0)
            elif mode == "I;16":
                pixels[x, y] = level * 257
            else:
                pixels[x, y] = level

    return image


def _write_truncated_jpeg(target: Path) -> None:
    """Write a JPEG cut off partway through, keeping a valid SOI header."""
    buffer = io.BytesIO()
    _gradient("RGB").save(buffer, "JPEG", quality=95)
    payload = buffer.getvalue()
    target.write_bytes(payload[: len(payload) * 6 // 10])


def _write_exif_rotated_jpeg(target: Path) -> None:
    """Write a JPEG whose EXIF declares orientation 6 without pre-rotating it."""
    image = _gradient("RGB")
    exif = image.getexif()
    exif[0x0112] = 6
    image.save(target, "JPEG", exif=exif)


def generate(target_root: Path) -> tuple[HardCase, ...]:
    """Materialize every hard case beneath `target_root`.

    Returns the case table so a caller can assert against the same
    expectations that produced the files.
    """
    target_root.mkdir(parents=True, exist_ok=True)

    _gradient("CMYK").save(target_root / "cmyk.jpg", "JPEG")
    _gradient("L").save(target_root / "grayscale.jpg", "JPEG")
    _gradient("I;16").save(target_root / "sixteen_bit.tiff", "TIFF")
    _gradient("RGBA").save(target_root / "alpha.png", "PNG")
    _write_exif_rotated_jpeg(target_root / "exif_rotated.jpg")
    _gradient("RGB").save(target_root / "uppercase_extension.JPG", "JPEG")
    _gradient("RGB").save(target_root / "fazenda São João.jpg", "JPEG")

    nested = target_root / "nested" / "level_a" / "level_b" / "level_c"
    nested.mkdir(parents=True, exist_ok=True)
    _gradient("RGB").save(nested / "deep.jpg", "JPEG")

    duplicate_buffer = io.BytesIO()
    _gradient("RGB").save(duplicate_buffer, "JPEG", quality=85)
    duplicate_bytes = duplicate_buffer.getvalue()
    (target_root / "duplicate_content_a.jpg").write_bytes(duplicate_bytes)
    (target_root / "duplicate_content_b.jpg").write_bytes(duplicate_bytes)

    (target_root / "zero_byte.jpg").write_bytes(b"")
    _write_truncated_jpeg(target_root / "truncated.jpg")

    _gradient("RGB").convert("P").save(target_root / "unsupported.gif", "GIF")
    (target_root / "unsupported.txt").write_text(
        "Not an image. Must never be discovered.\n", encoding="utf-8"
    )

    return HARD_CASES
