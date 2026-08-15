"""Re-encode source photographs to the demo dataset's committed-image spec.

Per RFC-022 section 8: roughly 1024px on the long edge, target under 200KB.
Also bakes in EXIF orientation and then drops all EXIF data -- which
deliberately strips GPS tags real aerial photographs often carry, since
committing a real property's coordinates would contradict the project's
local-first privacy stance (ARCHITECTURE.md section 2).

Usage:
    python -m dataset_tools.reencode <input> <output> [--max-edge 1024] [--max-kb 200]

`<input>`/`<output>` may each be a single file, or both may be directories
-- every supported image file directly inside `<input>` is then re-encoded
into `<output>` under the same stem with a `.jpg` extension.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

SOURCE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
MIN_QUALITY = 40


def reencode_one(source: Path, target: Path, max_edge: int, max_kb: int) -> None:
    """Re-encode a single image in place, downscaling and stripping EXIF."""
    opened = Image.open(source)
    # `exif_transpose` returns None only when called with in_place=True.
    image: Image.Image = ImageOps.exif_transpose(opened) or opened
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    width, height = image.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / longest
        new_size = (round(width * scale), round(height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    target.parent.mkdir(parents=True, exist_ok=True)

    quality = 90
    while True:
        image.save(target, "JPEG", quality=quality, optimize=True)
        if target.stat().st_size <= max_kb * 1024 or quality <= MIN_QUALITY:
            break
        quality -= 5

    size_kb = target.stat().st_size // 1024
    if size_kb > max_kb:
        print(
            f"warning: {target.name} still {size_kb}KB at quality {quality}",
            file=sys.stderr,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-edge", type=int, default=1024)
    parser.add_argument("--max-kb", type=int, default=200)
    args = parser.parse_args()

    if args.input.is_dir():
        args.output.mkdir(parents=True, exist_ok=True)
        sources = sorted(
            path
            for path in args.input.iterdir()
            if path.suffix.lower() in SOURCE_EXTENSIONS
        )
        if not sources:
            print(f"no image files found in {args.input}", file=sys.stderr)
            sys.exit(1)
        for source in sources:
            target = args.output / f"{source.stem}.jpg"
            reencode_one(source, target, args.max_edge, args.max_kb)
            print(f"{source.name} -> {target} ({target.stat().st_size // 1024}KB)")
    else:
        reencode_one(args.input, args.output, args.max_edge, args.max_kb)
        size_kb = args.output.stat().st_size // 1024
        print(f"{args.input.name} -> {args.output} ({size_kb}KB)")


if __name__ == "__main__":
    main()
