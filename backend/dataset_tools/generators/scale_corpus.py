"""Generate a large synthetic image corpus for indexing-throughput benchmarks.

Usage:
    python -m dataset_tools.generators.scale_corpus --count 100000 [--target PATH]

Unlike `hard_cases.py` (structural edge cases) or the committed demo
corpus (real photographs), this generator exists purely to produce
*scale* -- up to the 100,000-image target in `ARCHITECTURE.md` section 22.
Output is never committed (covered by the `data/` entry in `.gitignore`,
since the default target nests under it) and is sharded across
subdirectories, since a single directory holding 100k+ entries degrades
badly on common filesystems (NTFS included).

Content is deliberately minimal. `FakeEmbeddingModel` never opens a
file's pixels (RFC-022 section 7.3), and `FilesystemImageProvider` only
reads filesystem metadata, so nothing in today's pipeline benefits from
photographic detail here. Generating a tiny valid JPEG per entry keeps
100k-scale generation fast and disk-light while still producing files a
real image library can open -- unlike the zero-byte and truncated cases
in `hard_cases.py`, which are broken on purpose.

Running the actual 100k-scale benchmark this corpus supports is out of
scope for RFC-022 (section 14). `FakeEmbeddingModel`'s vectors only
stopped being degenerate once RFC-022 section 7.4 was fixed, and a
measured, reported benchmark run belongs in `scripts/benchmark.py`
(`ARCHITECTURE.md` section 22), not in this generator.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SCALE_TARGET = Path("data/scale")
DEFAULT_SHARD_SIZE = 1000
IMAGE_SIZE = (8, 8)
PROGRESS_INTERVAL = 10_000


def generate(
    target_root: Path, count: int, *, shard_size: int = DEFAULT_SHARD_SIZE
) -> None:
    """Generate `count` minimal, valid JPEG files under `target_root`, sharded.

    Deterministic: regenerating the same `count` produces byte-identical
    files, since each image's fill color is derived from its index alone.
    Shards into `target_root/00000/`, `target_root/00001/`, ... with at
    most `shard_size` files per subdirectory.
    """
    target_root.mkdir(parents=True, exist_ok=True)

    for index in range(count):
        shard = target_root / f"{index // shard_size:05d}"
        shard.mkdir(parents=True, exist_ok=True)

        color = (index % 256, (index * 7) % 256, (index * 13) % 256)
        image = Image.new("RGB", IMAGE_SIZE, color)
        image.save(shard / f"image_{index:07d}.jpg", "JPEG")

        if (index + 1) % PROGRESS_INTERVAL == 0:
            logger.info("Generated %d/%d synthetic images", index + 1, count)

    logger.info("Finished generating %d synthetic images in %s", count, target_root)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count", type=int, required=True, help="Number of images to generate"
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_SCALE_TARGET,
        help="Directory to generate images into (default: %(default)s)",
    )
    parser.add_argument(
        "--shard-size",
        type=int,
        default=DEFAULT_SHARD_SIZE,
        help="Maximum images per subdirectory (default: %(default)s)",
    )
    return parser


def main() -> None:
    """Parse CLI arguments and run `generate` against the resolved target."""
    args = _build_arg_parser().parse_args()
    generate(args.target.resolve(), args.count, shard_size=args.shard_size)


if __name__ == "__main__":
    main()
