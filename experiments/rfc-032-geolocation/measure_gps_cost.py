"""What does reading the GPS cost, and how much of the collection has one? Measure.

RFC-032 section 4.2 claims that reading the position in the *same open* as the
capture date costs a GPS-IFD parse, not a second file open, and section 2.3
left three numbers TBM. This script answers them with the project's own code:

1. **Cost.** Per file, on a synthetic corpus of real JPEGs carrying a real
   Exif sub-IFD and a real GPS IFD:

       date only        -- `read_capture_date()`: the RFC-028 path
       both, one open   -- `read_exif_facts()`: what the scan now calls
       both, two opens  -- the date reader, then the file opened again for the
                           GPS: the design section 4.2 rejected, measured so
                           the rejection is a number

   plus `FilesystemImageProvider.discover()` end to end, and a spy on
   `Image.open()` confirming one open per file.

2. **Coverage.** Over a folder of the real collection (`--root`), optionally a
   random sample of it (`--sample N`): how many files `read_exif_facts()`
   gives an `exif_gps` position, how many `unknown`, how many `0, 0`.

3. **The aircraft-to-subject offset** behind `MIN_RADIUS_M` (section 2.2).
   DJI writes `drone-dji:RelativeAltitude` and `drone-dji:GimbalPitchDegree` in
   the XMP packet. With the gimbal convention 0 = horizon, -90 = straight down,
   the ground point the camera's optical axis hits is `altitude / tan(|pitch|)`
   metres from the point the GPS recorded. The criterion is declared before the
   number: **`MIN_RADIUS_M` is the 95th percentile of that offset over the
   measured files, rounded up to the next 50 m.** A shot above the horizon
   has no ground point and is counted, not averaged in.

   The convention was checked against the files rather than assumed: a frame
   recording -17.9 shows the horizon near its top edge, one recording -6.7 is
   nearly level with sky in it. And one camera does not record the pitch at
   all: every FC3682 frame carries exactly `+0.00`, including frames that
   visibly look steeply down. Those are excluded from the offset and reported
   as such, not read as horizontal.

**What this does not measure, and says so in its output:** a cold read from a
spinning external disk (the corpus was just written, so it is in the OS cache);
and coverage beyond the folder it is pointed at.

Run it with the backend virtualenv, from the repository root:

    backend/.venv/Scripts/python.exe experiments/rfc-032-geolocation/measure_gps_cost.py --root "C:/path/to/photos"

`RFC032_SMALL_FILES` / `RFC032_LARGE_FILES` override the synthetic corpus sizes.
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import random
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

import PIL  # noqa: E402
from PIL import Image  # noqa: E402
from PIL.TiffImagePlugin import IFDRational  # noqa: E402

from app.infrastructure.config.constants import (  # noqa: E402
    SUPPORTED_IMAGE_EXTENSIONS,
)
from app.infrastructure.filesystem import exif_capture_date  # noqa: E402
from app.infrastructure.filesystem.exif_capture_date import (  # noqa: E402
    DATE_TIME_DIGITIZED,
    DATE_TIME_ORIGINAL,
    EXIF_IFD_POINTER,
    GPS_IFD_POINTER,
    GPS_LATITUDE,
    GPS_LATITUDE_REF,
    GPS_LONGITUDE,
    GPS_LONGITUDE_REF,
    read_capture_date,
    read_exif_facts,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    FilesystemImageProvider,
)

random.seed(0)

SMALL_FILES = int(os.environ.get("RFC032_SMALL_FILES", 3_000))
LARGE_FILES = int(os.environ.get("RFC032_LARGE_FILES", 200))
PROJECTED_FILES = 100_000
REPEATS = 3
XMP_HEAD_BYTES = 2 * 1024 * 1024
RADIUS_STEP_M = 50

XMP_RELATIVE_ALTITUDE = re.compile(rb'drone-dji:RelativeAltitude\s*=\s*"([^"]*)"')
XMP_GIMBAL_PITCH = re.compile(rb'drone-dji:GimbalPitchDegree\s*=\s*"([^"]*)"')


def rational(value: float, denominator: int = 10_000) -> IFDRational:
    return IFDRational(round(value * denominator), denominator)


def make_template(path: Path, size: tuple[int, int]) -> None:
    """One JPEG with a DJI-like Exif sub-IFD and GPS IFD, and noisy pixels."""
    width, height = size
    noise = Image.effect_noise((width // 4, height // 4), 64).convert("RGB")
    image = noise.resize(size)
    exif = Image.Exif()
    exif[0x010F] = "DJI"
    exif[0x0110] = "FC3682"
    sub_ifd = exif.get_ifd(EXIF_IFD_POINTER)
    sub_ifd[DATE_TIME_ORIGINAL] = "2018:07:14 15:32:05"
    sub_ifd[DATE_TIME_DIGITIZED] = "2018:07:14 15:32:05"
    gps = exif.get_ifd(GPS_IFD_POINTER)
    gps[GPS_LATITUDE_REF] = "S"
    gps[GPS_LATITUDE] = (rational(26), rational(19), rational(17.0615))
    gps[GPS_LONGITUDE_REF] = "W"
    gps[GPS_LONGITUDE] = (rational(48), rational(48), rational(58.7052))
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


def two_opens(path: Path) -> object:
    """The rejected design: the date reader, then a second open for the GPS."""
    read_capture_date(path)
    return read_exif_facts(path, capture_date=False, position=True)


def per_file_ms(paths: list[Path], operation: Callable[[Path], object]) -> list[float]:
    timings = []
    for path in paths:
        started = time.perf_counter()
        operation(path)
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def describe(label: str, timings: list[float]) -> float:
    ordered = sorted(timings)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    median = statistics.median(ordered)
    print(
        f"  {label:<16} median {median:7.3f} ms   mean "
        f"{statistics.fmean(ordered):7.3f} ms   p95 {p95:7.3f} ms   "
        f"max {ordered[-1]:8.3f} ms"
    )
    return median


def count_opens(root: Path) -> tuple[int, int]:
    """Discover `root` with both extractions on, counting `Image.open()` calls."""
    calls = 0
    real_open = exif_capture_date.Image.open

    def spy(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    exif_capture_date.Image.open = spy  # type: ignore[assignment]
    try:
        files = sum(
            1
            for _ in FilesystemImageProvider(
                root, SUPPORTED_IMAGE_EXTENSIONS
            ).discover()
        )
    finally:
        exif_capture_date.Image.open = real_open  # type: ignore[assignment]
    return files, calls


def measure_corpus(name: str, root: Path, paths: list[Path]) -> dict[str, float]:
    size_kb = paths[0].stat().st_size / 1024
    print(f"{name}: {len(paths)} files, {size_kb:.0f} KB each")

    for path in paths:  # warm-up, discarded: the first touch is metadata-cold
        read_exif_facts(path)

    medians: dict[str, list[float]] = {"date": [], "one": [], "two": []}
    for repeat in range(REPEATS):
        last = repeat == REPEATS - 1
        for key, label, operation in (
            ("date", "date only", read_capture_date),
            ("one", "both, one open", read_exif_facts),
            ("two", "both, two opens", two_opens),
        ):
            timings = per_file_ms(paths, operation)
            medians[key].append(statistics.median(timings))
            if last:
                describe(label, timings)

    sample = read_exif_facts(paths[0])
    print(f"  sanity: {sample}")

    for date, gps in ((False, False), (True, False), (True, True)):
        started = time.perf_counter()
        count = sum(
            1
            for _ in FilesystemImageProvider(
                root,
                SUPPORTED_IMAGE_EXTENSIONS,
                extract_capture_date=date,
                extract_gps=gps,
            ).discover()
        )
        elapsed = time.perf_counter() - started
        print(
            f"  discover(date={date!s:<5}, gps={gps!s:<5}) {count} files in "
            f"{elapsed:6.2f} s = {elapsed / count * 1000:6.3f} ms/file"
        )

    files, opens = count_opens(root)
    print(f"  Image.open() spy: {opens} opens for {files} files discovered")

    result = {key: statistics.median(values) for key, values in medians.items()}
    print(
        f"  medians across {REPEATS} repeats: date {result['date']:.3f} ms, "
        f"one open {result['one']:.3f} ms, two opens {result['two']:.3f} ms"
    )
    print()
    return result


def xmp_float(pattern: re.Pattern[bytes], head: bytes) -> float | None:
    match = pattern.search(head)
    if not match:
        return None
    try:
        return float(match.group(1).decode("ascii").strip())
    except ValueError:
        return None


def measure_collection(root: Path, sample: int) -> None:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    )
    unsupported = Counter(
        path.suffix.lower()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS
    )
    if sample and len(files) > sample:
        files = sorted(random.Random(0).sample(files, sample))
    print(f"real collection: {root}")
    print(f"  {len(files)} supported files examined (sample={sample or 'all'})")
    if unsupported:
        print(
            "  NOT examined, unsupported extension: "
            + ", ".join(
                f"{ext or '(none)'}={n}" for ext, n in unsupported.most_common()
            )
        )

    cold = per_file_ms(files, read_exif_facts)
    warm = per_file_ms(files, read_exif_facts)
    describe("first pass", cold)
    describe("second pass", warm)

    facts = [read_exif_facts(path) for path in files]
    positions = Counter(
        fact.position.source.value if fact.position else "not examined"
        for fact in facts
    )
    dates = Counter(
        fact.capture_date.source.value if fact.capture_date else "not examined"
        for fact in facts
    )
    total = len(files)
    print(
        "  position: "
        + ", ".join(
            f"{source}={n} ({100 * n / total:.1f}%)" for source, n in positions.items()
        )
    )
    print(
        "  date:     "
        + ", ".join(
            f"{source}={n} ({100 * n / total:.1f}%)" for source, n in dates.items()
        )
    )

    offsets: list[float] = []
    exactly_zero: Counter[str] = Counter()
    above_horizon = 0
    no_xmp = 0
    pitches_by_camera: dict[str, list[float]] = {}
    for path in files:
        with path.open("rb") as handle:
            head = handle.read(XMP_HEAD_BYTES)
        with Image.open(path) as image:
            camera = str(image.getexif().get(0x0110, "?")).strip("\x00 ")
        altitude = xmp_float(XMP_RELATIVE_ALTITUDE, head)
        pitch = xmp_float(XMP_GIMBAL_PITCH, head)
        if altitude is None or pitch is None:
            no_xmp += 1
            continue
        pitches_by_camera.setdefault(camera, []).append(pitch)
        if pitch == 0.0:
            # Not "horizontal". Every FC3682 file in the measured folder
            # records exactly +0.00, including frames that visibly look
            # steeply down (checked on DJI_0122 and DJI_0434): the value is
            # a placeholder, and no offset can be computed from it.
            exactly_zero[camera] += 1
            continue
        if pitch > 0.0:
            above_horizon += 1
            continue
        offsets.append(abs(altitude) / math.tan(math.radians(abs(pitch))))

    print()
    print("gimbal pitch by camera (drone-dji:GimbalPitchDegree):")
    for camera, pitches in sorted(pitches_by_camera.items()):
        print(
            f"  {camera:<10} {len(pitches):3d} files, pitch {min(pitches):+6.1f} .. "
            f"{max(pitches):+6.1f}, exactly 0.00 in {pitches.count(0.0)}"
        )
    print()
    print("aircraft-to-subject offset, altitude / tan(|gimbal pitch|):")
    print(
        f"  {len(offsets)} files with an offset; excluded: "
        f"{sum(exactly_zero.values())} with pitch exactly 0.00 "
        f"({', '.join(f'{c}={n}' for c, n in sorted(exactly_zero.items())) or '-'}), "
        f"{above_horizon} above the horizon, {no_xmp} without drone-dji "
        "altitude/pitch"
    )
    if offsets:
        ordered = sorted(offsets)

        def percentile(fraction: float) -> float:
            index = min(
                len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1)
            )
            return ordered[index]

        print(
            f"  min {ordered[0]:7.1f} m   p50 {percentile(0.50):7.1f} m   "
            f"p90 {percentile(0.90):7.1f} m   p95 {percentile(0.95):7.1f} m   "
            f"max {ordered[-1]:7.1f} m"
        )
        minimum = math.ceil(percentile(0.95) / RADIUS_STEP_M) * RADIUS_STEP_M
        print(
            f"  criterion: p95 rounded up to the next {RADIUS_STEP_M} m -> "
            f"MIN_RADIUS_M = {minimum} m"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, help="a folder of the real collection")
    parser.add_argument("--sample", type=int, default=0, help="random sample size")
    args = parser.parse_args()

    print(
        f"python {platform.python_version()}, Pillow {PIL.__version__}, "
        f"{platform.platform()}"
    )
    print(f"processor: {platform.processor()}")
    print()

    if args.root is not None:
        measure_collection(args.root, args.sample)

    workdir = Path(tempfile.mkdtemp(prefix="rfc032-gps-"))
    try:
        started = time.perf_counter()
        small = build_corpus(workdir / "small", (1920, 1080), SMALL_FILES)
        large = build_corpus(workdir / "large", (4000, 3000), LARGE_FILES)
        print(f"corpus built in {time.perf_counter() - started:.1f} s under {workdir}")
        print()

        results = [
            measure_corpus("1920x1080", workdir / "small", small),
            measure_corpus("4000x3000", workdir / "large", large),
        ]
        worst = {key: max(result[key] for result in results) for key in results[0]}
        print("projection (median of the slower corpus), per full scan of 100,000:")
        for key, label in (
            ("date", "date only (RFC-028)"),
            ("one", "both facts, one open"),
            ("two", "both facts, two opens"),
        ):
            print(
                f"  {label:<24} {worst[key]:.3f} ms x {PROJECTED_FILES} = "
                f"{worst[key] * PROJECTED_FILES / 1000:6.1f} s"
            )
        print(
            f"  GPS in the same open adds {worst['one'] - worst['date']:+.3f} ms/file; "
            f"a second open would add {worst['two'] - worst['date']:+.3f} ms/file"
        )
        print()
        print(
            "NOT MEASURED: cold reads from a spinning external disk. The synthetic "
            "corpus was in the OS cache; these are parsing costs, not seek costs."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
