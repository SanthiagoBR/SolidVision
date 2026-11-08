"""The regression set of RFC-042 section 5.1.3: COCO photos and captions.

Section 5.1.3 asks for general, non-aerial queries, to catch an adapter or a
fine-tune that makes ordinary search worse. COCO's captions are the standard
text-to-image retrieval benchmark: everyday scenes, five captions per photo,
written by people.

    python fetch_coco.py --to C:\\Users\\me\\rfc042\\coco

Downloads the official 2017 annotations (241 MB, once) and keeps only the
captions of the validation photos; then picks 1,000 validation photos in a
fixed pseudo-random order and downloads each one from the official image
server. Writes `coco_regression.jsonl`: one line per photo, with its file and
its five captions.

Only for measuring, on this machine: nothing from COCO is trained on or
distributed. The captions are CC BY 4.0; the photos belong to their Flickr
authors under the licenses COCO lists per photo, which the file keeps.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ANNOTATIONS = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
CAPTIONS = "annotations/captions_val2017.json"
IMAGES = "http://images.cocodataset.org/val2017/{file}"
PHOTOS = 1000
USER_AGENT = "SolidVision-RFC042-regression/1.0"


def fetch(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temporary = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=120) as response:
        temporary.write_bytes(response.read())
    temporary.replace(target)


def order(image_id: int) -> str:
    """A fixed pseudo-random order: the same 1,000 photos on every run."""
    return hashlib.sha256(f"coco-{image_id}".encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--to", type=Path, required=True, help="where to keep COCO")
    parser.add_argument("--photos", type=int, default=PHOTOS)
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    args.to.mkdir(parents=True, exist_ok=True)

    captions_path = args.to / "captions_val2017.json"
    if not captions_path.exists():
        archive = args.to / "annotations_trainval2017.zip"
        if not archive.exists():
            print(f"downloading {ANNOTATIONS}")
            fetch(ANNOTATIONS, archive)
        with zipfile.ZipFile(archive) as bundle:
            captions_path.write_bytes(bundle.read(CAPTIONS))
        archive.unlink()
    data = json.loads(captions_path.read_text(encoding="utf-8"))
    licenses = {license["id"]: license["name"] for license in data["licenses"]}
    texts: dict[int, list[str]] = {}
    for annotation in data["annotations"]:
        texts.setdefault(annotation["image_id"], []).append(
            " ".join(annotation["caption"].split())
        )
    images = sorted(data["images"], key=lambda image: order(image["id"]))
    chosen = [image for image in images if len(texts.get(image["id"], [])) >= 5]
    chosen = chosen[: args.photos]

    folder = args.to / "val2017"
    folder.mkdir(exist_ok=True)
    started = time.perf_counter()
    for number, image in enumerate(chosen, 1):
        target = folder / image["file_name"]
        if not target.exists():
            fetch(IMAGES.format(file=image["file_name"]), target)
        if number % 100 == 0:
            seconds = time.perf_counter() - started
            print(f"  {number}/{len(chosen)} photos, {seconds:.0f} s")

    with (args.to / "coco_regression.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as out:
        for image in chosen:
            record = {
                "image_id": image["id"],
                "path": f"val2017/{image['file_name']}",
                "captions": texts[image["id"]][:5],
                "license": licenses.get(image["license"], ""),
                "flickr_url": image.get("flickr_url", ""),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"wrote {args.to / 'coco_regression.jsonl'}: {len(chosen)} photos")


if __name__ == "__main__":
    main()
