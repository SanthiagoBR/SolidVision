"""Quick check: is the GPS recorded in a folder of photos usable?

Standalone on purpose -- no imports from the SolidVision project.

    python exif_gps_check.py "C:\\path\\to\\photos" [--max 500] [--show 8]

For each file it reads the header only and looks at two places a drone or
phone can record a position:

  - EXIF GPS: the GPS IFD, pointed to by tag 0x8825 in IFD0.
  - XMP GPS:  drone-dji:GpsLatitude / GpsLongitude. Checked, not assumed:
              the DJI files tested carry drone-dji altitude and attitude in
              XMP but no latitude or longitude, so XMP is a fallback that
              may never match on DJI.

It reports how many photos have a usable position, how many are placeholders
(0,0) or out of range, whether the two sources agree, and where the
coordinates cluster. Needs Pillow only (pip install pillow).
"""

from __future__ import annotations

import argparse
import math
import random
import re
import time
import warnings
from collections import Counter
from pathlib import Path

from PIL import Image

GPS_IFD_POINTER = 0x8825
MAKE_TAG = 0x010F
MODEL_TAG = 0x0110
GPS_LAT_REF, GPS_LAT, GPS_LON_REF, GPS_LON = 1, 2, 3, 4

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
    ".heic", ".heif", ".dng", ".arw", ".cr2", ".nef", ".raf", ".orf",
}

# XMP sits in the file header, so reading the first couple of MB is enough.
XMP_HEAD_BYTES = 2 * 1024 * 1024
XMP_LAT = re.compile(
    rb'drone-dji:GpsLatitude\s*=\s*"([^"]*)"|<drone-dji:GpsLatitude>([^<]*)<'
)
XMP_LON = re.compile(
    rb'drone-dji:GpsLongitude\s*=\s*"([^"]*)"|<drone-dji:GpsLongitude>([^<]*)<'
)

EARTH_RADIUS_M = 6_371_000


def dms_to_degrees(parts: object, ref: object) -> float | None:
    """EXIF stores degrees, minutes and seconds as rationals, plus N/S or E/W."""
    if not parts or ref is None:
        return None
    try:
        degrees, minutes, seconds = (float(p) for p in parts)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    value = degrees + minutes / 60 + seconds / 3600
    return -value if str(ref).strip().upper() in ("S", "W") else value


def exif_position(exif: Image.Exif) -> tuple[float | None, float | None] | None:
    gps = exif.get_ifd(GPS_IFD_POINTER)
    if not gps:
        return None
    return (
        dms_to_degrees(gps.get(GPS_LAT), gps.get(GPS_LAT_REF)),
        dms_to_degrees(gps.get(GPS_LON), gps.get(GPS_LON_REF)),
    )


def xmp_position(head: bytes) -> tuple[float | None, float | None] | None:
    lat = _xmp_float(XMP_LAT, head)
    lon = _xmp_float(XMP_LON, head)
    if lat is None and lon is None:
        return None
    return lat, lon


def _xmp_float(pattern: re.Pattern[bytes], blob: bytes) -> float | None:
    match = pattern.search(blob)
    if not match:
        return None
    raw = (match.group(1) or match.group(2) or b"").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def classify(position: tuple[float | None, float | None] | None) -> str:
    if position is None:
        return "absent"
    lat, lon = position
    if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
        return "absent"
    # 0,0 is what a drone or camera writes before it has a satellite fix.
    if abs(lat) < 1e-4 and abs(lon) < 1e-4:
        return "zero"
    if abs(lat) > 90 or abs(lon) > 180:
        return "out_of_range"
    return "valid"


def metres_apart(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def inspect(path: Path) -> dict:
    row: dict = {
        "name": path.name,
        "ext": path.suffix.lower(),
        "camera": "",
        "group": "other",
        "opened": False,
        "exif": None,
        "xmp": None,
        "error": None,
    }
    try:
        with path.open("rb") as handle:
            head = handle.read(XMP_HEAD_BYTES)
    except OSError as exc:
        row["error"] = f"read failed: {exc}"
        return row
    row["xmp"] = xmp_position(head)

    make = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(path) as image:
                exif = image.getexif()
                make = str(exif.get(MAKE_TAG, "")).strip()
                model = str(exif.get(MODEL_TAG, "")).strip()
                row["camera"] = f"{make} {model}".strip()
                row["exif"] = exif_position(exif)
                row["opened"] = True
    except Exception as exc:  # any malformed file; the reason is reported, not hidden
        row["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"

    is_dji = "DJI" in make.upper() or path.name.upper().startswith("DJI_")
    if is_dji or b"drone-dji" in head:
        row["group"] = "dji"
    return row


def preferred(row: dict) -> tuple[tuple[float, float] | None, str | None]:
    """EXIF first, XMP as fallback: the order a real pipeline would take."""
    for key in ("exif", "xmp"):
        if classify(row[key]) == "valid":
            return row[key], key
    return None, None


def fmt_states(counts: Counter, total: int) -> str:
    order = ("valid", "zero", "out_of_range", "absent")
    parts = [f"{state} {counts[state]} ({100 * counts[state] / total:.1f}%)" for state in order]
    return ", ".join(parts)


def report_failures(rows: list[dict]) -> None:
    opened = sum(r["opened"] for r in rows)
    print(f"\nOpened by Pillow: {opened}/{len(rows)}")
    failed = [r for r in rows if not r["opened"]]
    if not failed:
        return
    by_ext = Counter(r["ext"] for r in failed)
    print("  not opened, by extension: " + ", ".join(f"{e}={n}" for e, n in by_ext.most_common()))
    for r in failed[:3]:
        print(f"  e.g. {r['name']}: {r['error']}")


def report_groups(rows: list[dict]) -> None:
    for group in ("dji", "other"):
        members = [r for r in rows if r["group"] == group]
        opened = [r for r in members if r["opened"]]
        if not members:
            continue
        print(f"\n[{group}] {len(members)} files, {len(opened)} opened")
        if not opened:
            continue
        print("  EXIF GPS: " + fmt_states(Counter(classify(r["exif"]) for r in opened), len(opened)))
        print("  XMP GPS:  " + fmt_states(Counter(classify(r["xmp"]) for r in opened), len(opened)))
        usable = sum(1 for r in opened if preferred(r)[0] is not None)
        print(f"  usable from either source: {usable} ({100 * usable / len(opened):.1f}%)")


def report_agreement(rows: list[dict]) -> None:
    both = [
        r for r in rows
        if classify(r["exif"]) == "valid" and classify(r["xmp"]) == "valid"
    ]
    if not both:
        print("\nFiles with valid EXIF and valid XMP: none, so the sources cannot be compared.")
        return
    gaps = sorted(metres_apart(r["exif"], r["xmp"]) for r in both)
    print(
        f"\nEXIF vs XMP on {len(both)} files with both: "
        f"median gap {gaps[len(gaps) // 2]:.1f} m, max {gaps[-1]:.1f} m"
    )


def report_spread(rows: list[dict]) -> None:
    points = [preferred(r)[0] for r in rows]
    points = [p for p in points if p is not None]
    if not points:
        print("\nNo usable position in this folder.")
        return
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    min_lat, max_lat, min_lon, max_lon = min(lats), max(lats), min(lons), max(lons)
    height_km = metres_apart((min_lat, min_lon), (max_lat, min_lon)) / 1000
    width_km = metres_apart((min_lat, min_lon), (min_lat, max_lon)) / 1000
    print(
        f"\nBounding box of {len(points)} positions: lat {min_lat:.5f}..{max_lat:.5f}, "
        f"lon {min_lon:.5f}..{max_lon:.5f} ({width_km:.1f} km x {height_km:.1f} km)"
    )

    cells = Counter((round(lat, 3), round(lon, 3)) for lat, lon in points)
    top = cells.most_common(5)
    top_share = 100 * sum(n for _, n in top) / len(points)
    print(f"Distinct ~100 m cells: {len(cells)}. The 5 busiest hold {top_share:.1f}% of positions:")
    for (lat, lon), n in top:
        print(f"  {n:6d} photos near {lat:.3f}, {lon:.3f}")


def report_samples(rows: list[dict], count: int) -> None:
    print("\nSample positions (paste into Google Maps to check them against the photo):")
    shown = 0
    for r in rows:
        pos, source = preferred(r)
        if pos is None:
            continue
        print(f"  {r['name']}: {pos[0]:.6f}, {pos[1]:.6f} ({source})")
        shown += 1
        if shown >= count:
            break
    if shown == 0:
        print("  none")

    rejected = [r for r in rows if r["opened"] and preferred(r)[0] is None]
    if rejected:
        print(f"\nOpened files with no usable position ({len(rejected)}), first few:")
        for r in rejected[:count]:
            print(f"  {r['name']} [{r['camera'] or 'no camera tag'}]: "
                  f"exif={classify(r['exif'])}, xmp={classify(r['xmp'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether a folder's photo GPS is usable.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--max", type=int, default=0, help="random sample of N files (0 = all)")
    parser.add_argument("--show", type=int, default=8, help="how many sample rows to print")
    args = parser.parse_args()

    if not args.folder.is_dir():
        parser.error(f"not a folder: {args.folder}")

    files = [
        p for p in args.folder.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    if args.max and len(files) > args.max:
        files = random.Random(0).sample(files, args.max)
    if not files:
        parser.error("no image files found in that folder")

    by_ext = Counter(p.suffix.lower() for p in files)
    print(f"Files: {len(files)}")
    print("By extension: " + ", ".join(f"{e}={n}" for e, n in by_ext.most_common()))

    cameras = Counter()
    started = time.perf_counter()
    rows = []
    for path in files:
        row = inspect(path)
        rows.append(row)
        if row["camera"]:
            cameras[row["camera"]] += 1
    elapsed = time.perf_counter() - started
    print(f"Read {len(rows)} headers in {elapsed:.1f}s ({1000 * elapsed / len(rows):.2f} ms/file)")

    if cameras:
        print("\nCameras (make + model):")
        for camera, n in cameras.most_common(8):
            print(f"  {n:6d}  {camera}")

    report_failures(rows)
    report_groups(rows)
    report_agreement(rows)
    report_spread(rows)
    report_samples(rows, args.show)


if __name__ == "__main__":
    main()
