"""What does reading the EXIF capture date cost per file? Measure, do not assume.

RFC-028 section 6 puts `read_capture_date()` inside the scan, for every
discovered file -- including the ones the incremental check then skips -- and
expected the cost to be "orders of magnitude below" the ~450 ms per image of
inference RFC-024 measured. Section 11 lists the opposite as a risk: a small
per-file cost times 100,000 files can still be a large total. This script
answers both from numbers.

It builds a synthetic corpus of real JPEGs carrying a real EXIF block, then
times, per file:

    stat        -- what the scan already paid before RFC-028
    exif        -- `read_capture_date()` alone, the new cost
    discover    -- `FilesystemImageProvider.discover()` end to end, with
                   extraction on and off, which is the number an operator sees

and projects the EXIF cost onto 100,000 files.

Two corpora, because the claim that only the header is read is itself
something to check: if the cost grew with the size of the file, `Image.open()`
would be decoding more than a header.

**What this does not measure, and says so in its output:** a cold read from a
spinning external disk. Every file here was just written, so the operating
system's cache holds it; the numbers are CPU and parsing cost, not seek time.
Windows offers no unprivileged way to drop the file cache, and inventing a
seek penalty would be the deduction RFC-028 refuses.

Run it with the backend virtualenv, from the repository root:

    backend/.venv/Scripts/python.exe <this file>

`RFC028_SMALL_FILES` / `RFC028_LARGE_FILES` override the corpus sizes.
"""

from __future__ import annotations

import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

import PIL  # noqa: E402
from PIL import Image  # noqa: E402

from app.infrastructure.config.constants import (  # noqa: E402
    SUPPORTED_IMAGE_EXTENSIONS,
)
from app.infrastructure.filesystem.exif_capture_date import (  # noqa: E402
    DATE_TIME_DIGITIZED,
    DATE_TIME_ORIGINAL,
    EXIF_IFD_POINTER,
    read_capture_date,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    FilesystemImageProvider,
)

random.seed(0)

SMALL_FILES = int(os.environ.get("RFC028_SMALL_FILES", 5_000))
"""1920x1080 frames: enough files that per-file noise averages out."""

LARGE_FILES = int(os.environ.get("RFC028_LARGE_FILES", 300))
"""4000x3000 frames, the size of a 12 MP drone photo, fewer of them for disk space."""

PROJECTED_FILES = 100_000
INFERENCE_MS_PER_IMAGE = 450.0
"""RFC-024's measured CPU inference cost, the scale RFC-028 section 6 compares to."""

REPEATS = 3


def make_template(path: Path, size: tuple[int, int]) -> None:
    """One JPEG with camera-like EXIF and pixel noise, so it compresses realistically.

    Noise rather than a flat colour: a flat image compresses to a few
    kilobytes and would make the "large" corpus no larger than the small
    one, hiding exactly the effect the second corpus exists to reveal.
    """
    width, height = size
    noise = Image.effect_noise((width // 4, height // 4), 64).convert("RGB")
    image = noise.resize(size)
    exif = Image.Exif()
    exif[0x010F] = "DJI"
    exif[0x0110] = "FC3582"
    sub_ifd = exif.get_ifd(EXIF_IFD_POINTER)
    sub_ifd[DATE_TIME_ORIGINAL] = "2018:07:14 15:32:05"
    sub_ifd[DATE_TIME_DIGITIZED] = "2018:07:14 15:32:05"
    sub_ifd[0x829A] = 1 / 500
    image.save(path, "JPEG", quality=90, exif=exif.tobytes())


def build_corpus(root: Path, size: tuple[int, int], count: int) -> list[Path]:
    root.mkdir(parents=True)
    template = root / "template.jpg"
    make_template(template, size)
    paths = []
    for index in range(count):
        target = root / f"{index // 1000:03d}" / f"DJI_{index:06d}.JPG"
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(template, target)
        paths.append(target)
    template.unlink()
    return paths


def per_file_ms(paths: list[Path], operation: Callable[[Path], object]) -> list[float]:
    timings = []
    for path in paths:
        started = time.perf_counter()
        operation(path)
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def describe(label: str, timings: list[float]) -> None:
    ordered = sorted(timings)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    print(
        f"  {label:<10} median {statistics.median(ordered):7.3f} ms   "
        f"mean {statistics.fmean(ordered):7.3f} ms   p95 {p95:7.3f} ms   "
        f"max {ordered[-1]:8.3f} ms"
    )


def measure_corpus(name: str, root: Path, paths: list[Path]) -> float:
    size_kb = paths[0].stat().st_size / 1024
    print(f"{name}: {len(paths)} files, {size_kb:.0f} KB each")

    # Warm-up pass: the first touch of each file is filesystem-metadata cold
    # even when the bytes were just written, and it is discarded.
    for path in paths:
        read_capture_date(path)

    exif_medians = []
    for repeat in range(REPEATS):
        stat_timings = per_file_ms(paths, lambda path: path.stat())
        exif_timings = per_file_ms(paths, read_capture_date)
        exif_medians.append(statistics.median(exif_timings))
        if repeat == REPEATS - 1:
            describe("stat", stat_timings)
            describe("exif", exif_timings)

    results = {read_capture_date(path) for path in paths[:10]}
    print(f"  sanity: extracted {sorted(str(result) for result in results)}")

    for extract in (False, True):
        started = time.perf_counter()
        count = sum(
            1
            for _ in FilesystemImageProvider(
                root, SUPPORTED_IMAGE_EXTENSIONS, extract_capture_date=extract
            ).discover()
        )
        elapsed = time.perf_counter() - started
        print(
            f"  discover(extract_capture_date={extract!s:<5}) "
            f"{count} files in {elapsed:6.2f} s = {elapsed / count * 1000:6.3f} ms/file"
        )

    median = statistics.median(exif_medians)
    print(f"  exif median across {REPEATS} repeats: {median:.3f} ms")
    print()
    return median


def main() -> None:
    print(
        f"python {platform.python_version()}, Pillow {PIL.__version__}, "
        f"{platform.platform()}"
    )
    print(f"processor: {platform.processor()}")
    print()

    workdir = Path(tempfile.mkdtemp(prefix="rfc028-exif-"))
    try:
        started = time.perf_counter()
        small = build_corpus(workdir / "small", (1920, 1080), SMALL_FILES)
        large = build_corpus(workdir / "large", (4000, 3000), LARGE_FILES)
        print(f"corpus built in {time.perf_counter() - started:.1f} s under {workdir}")
        print()

        small_ms = measure_corpus("1920x1080", workdir / "small", small)
        large_ms = measure_corpus("4000x3000", workdir / "large", large)

        worst = max(small_ms, large_ms)
        projected_s = worst * PROJECTED_FILES / 1000
        print("projection (median of the slower corpus):")
        print(
            f"  {PROJECTED_FILES} files x {worst:.3f} ms = {projected_s:.1f} s "
            f"({projected_s / 60:.1f} min) of EXIF reading per full scan"
        )
        print(
            f"  inference at {INFERENCE_MS_PER_IMAGE:.0f} ms/image would be "
            f"{INFERENCE_MS_PER_IMAGE * PROJECTED_FILES / 3_600_000:.1f} h for the "
            f"same files; ratio {INFERENCE_MS_PER_IMAGE / worst:.0f}x"
        )
        print()
        print(
            "NOT MEASURED: cold reads from a spinning external disk. Every file "
            "above was in the OS cache; these are parsing costs, not seek costs."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
