"""An evaluation set from the user's own archive, for queries written by hand.

The Commons evaluation set (RFC-042 section 5.1.2) is wide scenes: towns,
coasts, landscapes. The archive the product searches is mostly drone photos
of one property, often from close by, and an adapter that helps on Commons
may not help there. This script samples drone photos from the archive into a
folder `review_captions.py` can open, so the person writes queries for them
in the same page, and the adapter is measured where it will be used.

    python sample_archive.py scan --archive E:/ --training D:/
        --training "E:/$RECYCLE.BIN" --out C:/Users/me/rfc042/archive
    python sample_archive.py sample --days 300 --out C:/Users/me/rfc042/archive
    python review_captions.py --out C:/Users/me/rfc042/archive
        --captions C:/Users/me/rfc042/archive/archive_queries.jsonl --port 8743

`scan` lists the archive's photo files once (the slow part, on an external
disk) and reads the capture day and position of every training photo.
`sample` picks the photos and writes 1,600 px copies for the page; run again
with a larger `--days`, it adds days and keeps the ones already chosen.

A second, separate sample goes in its own folder, with a copy of
`scan.json`. `--exclude-sample` keeps out the days of another sample's
manifest, and `--pageid-base` keeps its pageids apart, because caches are
keyed by pageid:

    python sample_archive.py sample --days 80 --out C:/Users/me/rfc042/archive-human
        --exclude-sample C:/Users/me/rfc042/archive/archive_sample.jsonl
        --pageid-base 9100000000

The rules:

- **Drone photos only:** a file named `DJI*` whose EXIF maker is DJI, or
  Hasselblad (DJI's camera on the Mavic 2 Pro).
- **One photo per capture day,** and the day is the series: near-identical
  shots of one flight never sit on both sides of a split, and no day votes
  twice. A day is drawn through the folders and file dates, so a month folder
  with many flights is not one draw.
- **Never a day with training material** (the `--training` folders: 2018 on
  `D:`, the recycle bin), never a year in `--exclude-year` (2018, part of it
  still on the archive), and never a date before `--min-year` (a drone whose
  clock reset to 2008 says nothing about the day).
- **A fixed seed:** the same archive gives the same sample.

Everything stays in `--out`, outside the repository: the thumbnails, the
paths and the queries are the user's own. Nothing leaves this computer.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_commons as commons  # noqa: E402

PHOTO = {".jpg", ".jpeg"}
SYSTEM = {"$recycle.bin", "system volume information"}
DRONE_MAKERS = ("dji", "hasselblad")
SCAN_NAME = "scan.json"
MANIFEST_NAME = "archive_sample.jsonl"
THUMBNAIL = 1600
PROBES_PER_GROUP = 3
# Far above any Commons pageid (~1.8e8): an archive photo is never mistaken
# for a Commons one if two ledgers ever meet.
PAGEID_BASE = 9_000_000_000
SEED = 20261104


def walk(root: Path, skip_system: bool) -> Iterator[os.DirEntry[str]]:
    """Every JPEG under `root`, with the stat the directory listing already has."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            print(f"  cannot list {directory}: {error}", file=sys.stderr)
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                top = Path(entry.path).parent == root
                if skip_system and top and entry.name.casefold() in SYSTEM:
                    continue
                stack.append(Path(entry.path))
            elif Path(entry.name).suffix.casefold() in PHOTO:
                if entry.name.startswith("$I"):  # recycle bin's index files
                    continue
                yield entry


def rational(value: Any) -> float:
    return (
        float(value[0]) / float(value[1]) if isinstance(value, tuple) else float(value)
    )


def read_exif(image: Image.Image) -> dict[str, Any]:
    exif = image.getexif()
    taken = exif.get_ifd(0x8769).get(36867) or exif.get(306) or ""
    found: dict[str, Any] = {
        "make": str(exif.get(271, "")).strip("\x00 "),
        "model": str(exif.get(272, "")).strip("\x00 "),
        "taken": str(taken).strip("\x00 "),
        "day": commons.first_date(str(taken)),
        "gps": None,
    }
    gps = exif.get_ifd(0x8825)
    try:
        latitude = sum(rational(part) / 60**n for n, part in enumerate(gps[2]))
        longitude = sum(rational(part) / 60**n for n, part in enumerate(gps[4]))
        if gps.get(1) == "S":
            latitude = -latitude
        if gps.get(3) == "W":
            longitude = -longitude
        valid = math.isfinite(latitude) and math.isfinite(longitude)
        if valid and (latitude or longitude):
            found["gps"] = [round(latitude, 6), round(longitude, 6)]
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
        pass
    return found


def probe(path: Path) -> dict[str, Any] | None:
    """Maker, model, capture day and position, from the header only."""
    try:
        with Image.open(path) as image:
            return read_exif(image)
    except (OSError, SyntaxError, ValueError) as error:
        print(f"  unreadable {path}: {type(error).__name__}", file=sys.stderr)
        return None


def metres(a: list[float], b: list[float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
def scan(args: argparse.Namespace) -> None:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    archive = Path(args.archive)
    started = time.perf_counter()
    photos = 0
    prefixes: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for entry in walk(archive, skip_system=True):
        photos += 1
        prefixes[entry.name[:4].upper()] += 1
        if entry.name.casefold().startswith("dji"):
            stat = entry.stat()
            candidates.append(
                {
                    "path": str(Path(entry.path).relative_to(archive)),
                    "bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
        if photos % 20000 == 0:
            print(f"  {photos} photos listed, {time.perf_counter() - started:.0f} s")
    listed = time.perf_counter() - started
    print(
        f"archive: {photos} JPEGs, {len(candidates)} named DJI*,"
        f" listed in {listed:.0f} s"
    )
    print("  name prefixes:", dict(prefixes.most_common(12)))

    training: dict[str, Any] = {}
    days: set[str] = set()
    points: list[list[float]] = []
    for folder in args.training:
        started = time.perf_counter()
        count = 0
        folder_days: Counter[str] = Counter()
        for entry in walk(
            Path(folder), skip_system=Path(folder).parent == Path(folder)
        ):
            found = probe(Path(entry.path))
            if found is None:
                continue
            count += 1
            if found["day"]:
                folder_days[found["day"]] += 1
            if found["gps"]:
                points.append(found["gps"])
        days |= set(folder_days)
        training[folder] = {"photos": count, "days": len(folder_days)}
        print(
            f"training {folder}: {count} photos on {len(folder_days)} days,"
            f" read in {time.perf_counter() - started:.0f} s"
        )

    scan_path = out / SCAN_NAME
    scan_path.write_text(
        json.dumps(
            {
                "archive": str(archive),
                "scanned_at": commons.utc_now(),
                "photos": photos,
                "prefixes": dict(prefixes.most_common(40)),
                "candidates": candidates,
                "training": training,
                "training_days": sorted(days),
                "training_points": points,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"wrote {scan_path}")


# --------------------------------------------------------------------------
def load_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def file_day(candidate: dict[str, Any]) -> str:
    """The file's modification day; Windows refuses dates before 1970."""
    try:
        return datetime.date.fromtimestamp(candidate["mtime"]).isoformat()
    except (OSError, OverflowError, ValueError):
        return "?"


def groups_in_order(
    candidates: list[dict[str, Any]], seed: int
) -> list[list[dict[str, Any]]]:
    """Folder and file date: a cheap stand-in for a flight, in a seeded order."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        groups[(str(Path(candidate["path"]).parent), file_day(candidate))].append(
            candidate
        )
    rng = random.Random(seed)
    ordered = [sorted(groups[key], key=lambda c: c["path"]) for key in sorted(groups)]
    rng.shuffle(ordered)
    for group in ordered:
        rng.shuffle(group)
    return ordered


def write_thumbnail(source: Path, target: Path) -> tuple[str, int, int, int]:
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        image.draft("RGB", (THUMBNAIL, THUMBNAIL))
        small = ImageOps.exif_transpose(image).convert("RGB")
        small.thumbnail((THUMBNAIL, THUMBNAIL))
        target.parent.mkdir(parents=True, exist_ok=True)
        small.save(target, "JPEG", quality=88)
    return digest, len(data), width, height


def sample(args: argparse.Namespace) -> None:
    out: Path = args.out
    scan_data = json.loads((out / SCAN_NAME).read_text(encoding="utf-8"))
    archive = Path(scan_data["archive"])
    manifest_path = out / MANIFEST_NAME
    chosen = load_manifest(manifest_path)
    taken_days = {record["day"] for record in chosen}
    excluded_days = set(scan_data["training_days"])
    other_days = {
        record["day"]
        for other in args.exclude_sample
        for record in load_manifest(other)
    }
    excluded_years = {str(year) for year in args.exclude_year}
    db = commons.open_database(out / commons.DATABASE_NAME)

    reasons: Counter[str] = Counter()
    probes = 0
    started = time.perf_counter()
    for group in groups_in_order(scan_data["candidates"], args.seed):
        if len(chosen) >= args.days:
            break
        for attempt, candidate in enumerate(group[:PROBES_PER_GROUP]):
            source = archive / candidate["path"]
            found = probe(source)
            probes += 1
            if found is None:
                reasons["unreadable"] += 1
                continue
            day = found["day"]
            if not found["make"].casefold().startswith(DRONE_MAKERS):
                reasons["not a drone camera"] += 1
                continue
            if not day:
                reasons["no capture date"] += 1
                continue
            if day[:4] in excluded_years or int(day[:4]) < args.min_year:
                reasons["excluded year"] += 1
                break  # the whole group shares the day
            if day in excluded_days:
                reasons["day has training photos"] += 1
                break
            if day in taken_days:
                reasons["day already chosen"] += 1
                break
            if day in other_days:
                reasons["day in the other sample"] += 1
                break
            number = len(chosen) + 1
            pageid = args.pageid_base + number
            relative = f"images/{pageid}.jpg"
            digest, size, width, height = write_thumbnail(source, out / relative)
            camera = f"{found['make']} {found['model']}".strip()
            record = {
                "pageid": pageid,
                "path": relative,
                "source": candidate["path"],
                "sha256": digest,
                "bytes": size,
                "day": day,
                "taken": found["taken"],
                "camera": camera,
                "gps": found["gps"],
                "probe": attempt + 1,
            }
            info = {
                "extmetadata": {
                    "Artist": camera,
                    "DateTimeOriginal": found["taken"],
                    "ImageDescription": f"{found['taken']} · {camera}",
                    "LicenseShortName": "acervo próprio",
                    "Categories": "",
                },
                "descriptionurl": "",
            }
            now = commons.utc_now()
            db.execute(
                "INSERT OR REPLACE INTO files (pageid, title, root, category, depth,"
                " discovered_at, described_at, info, decision, reason, path, bytes,"
                " sha256, width, height, grayscale, downloaded_at)"
                " VALUES (?, ?, 'archive', ?, 0, ?, ?, ?, 'accepted', '', ?, ?, ?,"
                " ?, ?, 0, ?)",
                (
                    pageid,
                    candidate["path"],
                    str(Path(candidate["path"]).parent),
                    now,
                    now,
                    json.dumps(info, ensure_ascii=False),
                    relative,
                    size,
                    digest,
                    width,
                    height,
                    now,
                ),
            )
            db.commit()
            with manifest_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            chosen.append(record)
            taken_days.add(day)
            if len(chosen) % 25 == 0:
                seconds = time.perf_counter() - started
                print(f"  {len(chosen)} chosen, {probes} probes, {seconds:.0f} s")
            break
    db.close()

    print(
        f"chosen: {len(chosen)} photos on {len(taken_days)} days"
        f" ({probes} files probed this run, {time.perf_counter() - started:.0f} s)"
    )
    print("  set aside:", dict(reasons.most_common()))
    print("  by year:", dict(sorted(Counter(r["day"][:4] for r in chosen).items())))
    print("  by camera:", dict(Counter(r["camera"] for r in chosen).most_common(8)))
    points = scan_data["training_points"]
    near: Counter[str] = Counter()
    for record in chosen:
        if not record["gps"]:
            near["no position"] += 1
            continue
        closest = min((metres(record["gps"], p) for p in points), default=math.inf)
        for limit in (100, 300, 1000):
            if closest <= limit:
                near[f"<= {limit} m"] += 1
                break
        else:
            near["farther"] += 1
    print("  nearest training photo:", dict(near))


def main() -> None:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="list the archive and the training days")
    scan_parser.add_argument("--archive", required=True)
    scan_parser.add_argument("--training", action="append", default=[])
    scan_parser.add_argument("--out", type=Path, required=True)
    sample_parser = sub.add_parser("sample", help="pick photos, write thumbnails")
    sample_parser.add_argument("--days", type=int, default=300)
    sample_parser.add_argument("--exclude-year", type=int, action="append", default=[])
    sample_parser.add_argument("--min-year", type=int, default=2010)
    sample_parser.add_argument("--seed", type=int, default=SEED)
    sample_parser.add_argument("--out", type=Path, required=True)
    sample_parser.add_argument(
        "--exclude-sample",
        type=Path,
        action="append",
        default=[],
        help="another sample's manifest, whose days are left out",
    )
    sample_parser.add_argument("--pageid-base", type=int, default=PAGEID_BASE)
    args = parser.parse_args()
    if args.command == "scan":
        scan(args)
    else:
        if not args.exclude_year:
            args.exclude_year = [2018]
        sample(args)


if __name__ == "__main__":
    main()
