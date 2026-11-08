"""Freeze RFC-042's real-archive evaluation set into `archive_eval_set.jsonl`.

One line per photo of `sample_archive.py`'s sample that has a reviewed
query: the query in Portuguese, where it came from, how far the photo was
taken from the nearest training photo, and whether it lands in the
validation or the test split.

    python freeze_archive_set.py --archive C:\\Users\\me\\rfc042\\archive

Everything is read from and written to the archive folder, outside the
repository, because the photos and their queries show private places:

- `archive_queries.jsonl`: the review page's ledger (the last line per
  photo and kind counts);
- `archive_sample.jsonl`: each photo's thumbnail, day, camera and GPS;
- `scan.json`: the GPS of every training photo.

**Origin.** The person wrote some queries; the others are Claude's drafts,
which the person approved as they were or edited. The three are kept apart,
because a query a model drafted while looking at the photo may be easier
for another model than one a person types.

**Near training.** The training photos are from other days, but some were
taken over the same places. A photo within `NEAR_METRES` of a training
photo is marked, so results can be read with and without those photos.

**The split.** As in `freeze_eval_set.py`: a third goes to validation and
the rest to test, in the collector's `spread(pageid)` order inside each
stratum (origin, near training). The test split is opened only for the
final evaluation (section 4.6). Both splits share one gallery: every photo
in this file.

**A set measured once.** `--one-split test` puts every query in the test
split, for a set read a single time, such as the person's own queries of a
second sample. `--ledger` and `--kind` read another ledger: queries the
person typed in the page's "write" mode are `caption` records whose text is
in `text`, not `pt`.

    python freeze_archive_set.py --archive C:\\Users\\me\\rfc042\\archive-human ^
        --ledger human_queries.jsonl --kind caption --one-split test

The file is refused if it already exists: a frozen set is replaced on
purpose, with `--force`, never by a rerun.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from collect_commons import spread  # noqa: E402

VALIDATION_SHARE = 3  # every third query of a stratum goes to validation
NEAR_METRES = 300
EARTH_METRES = 6_371_000.0
ORIGINS = ("human", "draft-approved", "draft-edited")
TEXT_FIELDS = {"query": "pt", "caption": "text"}  # where each kind keeps its text


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def latest_queries(ledger: Path, kind: str = "query") -> dict[int, dict[str, Any]]:
    field = TEXT_FIELDS[kind]
    latest: dict[int, dict[str, Any]] = {}
    for record in read_jsonl(ledger):
        if record.get("kind") == kind:
            latest[int(record["pageid"])] = record
    return {
        pageid: {**record, "pt": str(record.get(field, ""))}
        for pageid, record in latest.items()
        if not record.get("deleted") and str(record.get(field, "")).strip()
    }


def origin_of(record: dict[str, Any]) -> str:
    source, verdict = record.get("source"), record.get("verdict")
    if source == "human":
        return "human"
    if source == "model draft" and verdict in ("approved", "edited"):
        return f"draft-{verdict}"
    raise SystemExit(f"{record['pageid']}: unknown origin {source!r}, {verdict!r}")


def metres(a: Sequence[float], b: Sequence[float]) -> float:
    """Great-circle distance between two (latitude, longitude) points."""
    lat1, lon1, lat2, lon2 = (math.radians(x) for x in (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_METRES * math.asin(math.sqrt(h))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--archive", type=Path, required=True, help="sample_archive.py's folder"
    )
    parser.add_argument(
        "--ledger", default="archive_queries.jsonl", help="ledger in the folder"
    )
    parser.add_argument("--kind", choices=sorted(TEXT_FIELDS), default="query")
    parser.add_argument(
        "--one-split", choices=("test",), help="put every query in this split"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing frozen set"
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    target = args.archive / "archive_eval_set.jsonl"
    if target.exists() and not args.force:
        raise SystemExit(
            f"{target.name} exists and is frozen; pass --force to replace it"
        )

    queries = latest_queries(args.archive / args.ledger, args.kind)
    sample = {
        int(r["pageid"]): r for r in read_jsonl(args.archive / "archive_sample.jsonl")
    }
    unknown = sorted(set(queries) - set(sample))
    if unknown:
        raise SystemExit(f"queries for photos not in the sample: {unknown[:5]}")
    scan = json.loads((args.archive / "scan.json").read_text(encoding="utf-8"))
    training: list[list[float]] = scan["training_points"]

    rows = []
    for pageid, query in queries.items():
        photo = sample[pageid]
        if not (args.archive / photo["path"]).is_file():
            raise SystemExit(f"{pageid}: thumbnail {photo['path']} is missing")
        gps = photo.get("gps")
        distance = round(min(metres(gps, p) for p in training)) if gps else None
        rows.append(
            {
                "pageid": pageid,
                "path": photo["path"],
                "day": photo["day"],
                "camera": photo["camera"],
                "origin": origin_of(query),
                "near_training_m": distance,
                "near": distance is not None and distance <= NEAR_METRES,
                "pt": " ".join(str(query["pt"]).split()),
            }
        )

    strata: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for row in rows:
        strata.setdefault((row["origin"], row["near"]), []).append(row)
    for members in strata.values():
        members.sort(key=lambda row: spread(row["pageid"]))
        for index, row in enumerate(members):
            row["split"] = "validation" if index % VALIDATION_SHARE == 0 else "test"
            if args.one_split:
                row["split"] = args.one_split

    rows.sort(key=lambda row: spread(row["pageid"]))
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            ordered = {"pageid": row["pageid"], "split": row.pop("split"), **row}
            handle.write(json.dumps(ordered, ensure_ascii=False) + "\n")

    frozen = list(read_jsonl(target))
    for split in ("validation", "test"):
        members = [r for r in frozen if r["split"] == split]
        origins = Counter(r["origin"] for r in members)
        parts = ", ".join(f"{origin} {origins[origin]}" for origin in ORIGINS)
        near = sum(r["near"] for r in members)
        print(f"{split}: {len(members)} queries ({parts}); within", end="")
        print(f" {NEAR_METRES} m of a training photo {near}")
    no_gps = sum(r["near_training_m"] is None for r in frozen)
    print(f"gallery {len(frozen)} photos; without GPS {no_gps}; wrote {target}")


if __name__ == "__main__":
    main()
