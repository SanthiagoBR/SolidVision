"""Training captions for RFC-042, written by Claude through the Message Batches API.

RFC-042 section 4.3: a vision-language model writes the descriptive caption of
each training photo, with the Commons title as a context hint, and a sample
of 5 to 10% is reviewed by hand. The model is Claude Sonnet 5.5, the one whose
216 captions the user reviewed at a 5.1% error rate (section 15, item 4).

    set PY=C:\\Users\\me\\rfc042\\venv-api\\Scripts\\python.exe
    %PY% caption_batch.py --out C:\\Users\\me\\rfc042\\commons plan
    %PY% caption_batch.py --out ... submit --pilot 40
    %PY% caption_batch.py --out ... status
    %PY% caption_batch.py --out ... collect
    %PY% caption_batch.py --out ... submit --all

Runs on its own environment (`anthropic`, `pillow`), not the app's.

**Which photos.** The training pool: accepted, downloaded photos whose series
(author and day) has no evaluation photo (`eval_set.jsonl`), and that have no
caption yet, by hand or by a model. In the collector's `spread` order.
`--pilot N` sends N/2 photos the user already reviewed, to compare the new
caption with the reviewed one, and N/2 new ones; only new ones become captions.

**What is kept.** Each batch's raw results go to `OUT/caption_results/`, with
the usage of every request. New captions are appended to
`OUT/vlm_captions.jsonl`, where the review page reads them, with the model, the
batch and the prompt version. Failures go to `OUT/caption_errors.jsonl`.
`OUT/caption_batches.json` remembers every batch sent, written right after
each one is created, so that nothing sent is lost if the script stops.

**Credentials.** `ANTHROPIC_API_KEY` from the environment or, on Windows, from
the user's environment in the registry, so a key set in "Edit environment
variables for your account" works without restarting the editor. The key is
never printed nor written anywhere.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
MODEL = "claude-sonnet-5-5"
EFFORT = "low"
MAX_TOKENS = 2048  # thinking counts toward it; a caption is ~40 tokens
PROMPT_VERSION = "v2"
# v1 (the first pilot, 40 photos): 27 to 39 words, median 32, over its own
# limit of 30, and 8 of 20 opened with "An aerial view of". v2 asks for the
# length of the captions the user approved (median 23) and drops the opener.
# Batch prices for Claude Sonnet 5.5: half of $2 / $10 per million tokens.
PRICE_INPUT = 1.00 / 1_000_000
PRICE_OUTPUT = 5.00 / 1_000_000
BATCH_REQUESTS = 1000  # ~100 MB of images per batch; the API allows 256 MB
CAPTION_WORDS = (8, 45)  # outside this range a caption is kept but flagged

SYSTEM = """\
You write one caption for a photo taken from the air: from a drone, a plane or \
a high viewpoint. The captions teach an image search model what these photos \
show, and people search it in plain words.

Write one sentence of about 20 words, never more than 25, in plain American \
English. Start with the main scene or subject, then the few most visible \
elements and how they are laid out. Add the light, the weather or the season \
only when they stand out. Do not open with "An aerial view of" or "A top-down \
view of": every photo here is seen from above, so mention the viewpoint only \
when it is unusual, such as through a plane window.

- Describe only what the photo shows. The file title comes with the photo as \
a hint for reading it, such as what kind of building something is; never state \
something because the title says it and the photo does not show it.
- Do not name places, landmarks, brands, people or events, and do not guess \
what a building is used for unless it is plain to see.
- Be careful with colors and with water: a river, a lake, a reservoir and the \
sea look different; write "water" when you cannot tell.
- Count only up to three; for more, say "several" or "a group of".
- Prefer common words to technical ones.
- Answer with the caption only, without quotes or a preface.

Examples of good captions:
A wide avenue with trees on its left side and a blue-roofed gas station, \
running between low houses and open scrubland.
A two-lane road running through a pine forest beside a wide bay, with a few \
houses and an industrial building on the right.
A natural lake with irregular shores, small wooded islands and a sandy beach, \
surrounded by green farmland and forest."""
PROMPT_SHA = hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Photo:
    pageid: int
    path: Path
    title: str
    series: str


def load_collector() -> ModuleType:
    """`collect_commons.py`, for the experiment's `spread` and `series_key`."""
    spec = importlib.util.spec_from_file_location(
        "collect_commons", HERE / "collect_commons.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("collect_commons.py not found next to this script")
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_commons"] = module
    spec.loader.exec_module(module)
    return module


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def append_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def photos(out: Path) -> list[Photo]:
    """Accepted, downloaded photos, in `spread` order."""
    collector = load_collector()
    uri = f"file:{(out / 'collection.sqlite').as_posix()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    found = [
        Photo(
            pageid=int(pageid),
            path=out / str(path),
            title=str(title).removeprefix("File:"),
            series=collector.series_key(json.loads(info)),
        )
        for pageid, path, title, info in db.execute(
            "SELECT pageid, path, title, info FROM files"
            " WHERE downloaded_at IS NOT NULL AND decision = 'accepted'"
        )
    ]
    found.sort(key=lambda photo: collector.spread(photo.pageid))
    return found


def captioned(out: Path) -> tuple[set[int], set[int]]:
    """Photos with any caption, and those whose model caption the user reviewed."""
    vlm = {int(r["pageid"]) for r in read_jsonl(out / "vlm_captions.jsonl")}
    ledger = list(read_jsonl(HERE / "captions.jsonl"))
    hand = {int(r["pageid"]) for r in ledger if r.get("kind") == "caption"}
    reviewed = {int(r["pageid"]) for r in ledger if r.get("kind") == "review"}
    return vlm | hand, reviewed


def training_pool(out: Path) -> tuple[list[Photo], list[Photo], list[Photo]]:
    """(the whole pool, the photos still to caption, those the user reviewed)."""
    evaluation = {r["series"] for r in read_jsonl(HERE / "eval_set.jsonl")}
    pool = [photo for photo in photos(out) if photo.series not in evaluation]
    done, reviewed = captioned(out)
    todo = [photo for photo in pool if photo.pageid not in done]
    return pool, todo, [photo for photo in pool if photo.pageid in reviewed]


def media_type(data: bytes) -> str:
    """From the file's first bytes: the collector kept what Commons served."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("not a JPEG, PNG or WebP image")


def request_params(photo: Photo) -> dict[str, Any]:
    data = photo.path.read_bytes()
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "output_config": {"effort": EFFORT},
        "system": SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type(data),
                            "data": base64.standard_b64encode(data).decode("ascii"),
                        },
                    },
                    {
                        "type": "text",
                        "text": f"Commons file title (a hint): {photo.title}",
                    },
                ],
            }
        ],
    }


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                key = str(winreg.QueryValueEx(handle, "ANTHROPIC_API_KEY")[0])
        except OSError:
            key = ""
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set, neither in this process nor in the"
            " user's environment"
        )
    return key


def client() -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=api_key(), timeout=600.0, max_retries=4)


def state_path(out: Path) -> Path:
    return out / "caption_batches.json"


def load_state(out: Path) -> list[dict[str, Any]]:
    path = state_path(out)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_state(out: Path, state: list[dict[str, Any]]) -> None:
    path = state_path(out)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
    temporary.replace(path)


def estimate(todo: list[Photo]) -> None:
    """Tokens and cost without calling the API: images at (w*h)/750 tokens."""
    from PIL import Image

    image_tokens = 0
    for photo in todo:
        with Image.open(photo.path) as image:
            image_tokens += image.size[0] * image.size[1] // 750
    text_tokens = (len(SYSTEM) // 4 + 25) * len(todo)  # rough: 4 characters a token
    tokens_in = image_tokens + text_tokens
    print(f"to caption: {len(todo)} photos, about {tokens_in / 1e6:.2f} M input tokens")
    for label, per_caption in (("captions only", 40), ("with some thinking", 300)):
        tokens_out = per_caption * len(todo)
        cost = tokens_in * PRICE_INPUT + tokens_out * PRICE_OUTPUT
        millions = tokens_out / 1e6
        print(f"  {label:20} ~{millions:.2f} M output tokens, about US$ {cost:.2f}")
    batches = -(-len(todo) // BATCH_REQUESTS)
    print(f"  {batches} batch(es) of up to {BATCH_REQUESTS} requests")


def submit(out: Path, chosen: list[Photo], kind: str) -> None:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    api = client()
    state = load_state(out)
    for start in range(0, len(chosen), BATCH_REQUESTS):
        chunk = chosen[start : start + BATCH_REQUESTS]
        requests = [
            Request(
                custom_id=f"p{photo.pageid}",
                params=MessageCreateParamsNonStreaming(**request_params(photo)),  # type: ignore[typeddict-item]
            )
            for photo in chunk
        ]
        batch = api.messages.batches.create(requests=requests)
        state.append(
            {
                "id": batch.id,
                "kind": kind,
                "created": datetime.now(UTC).isoformat(),
                "model": MODEL,
                "effort": EFFORT,
                "prompt": PROMPT_VERSION,
                "prompt_sha": PROMPT_SHA,
                "pageids": [photo.pageid for photo in chunk],
                "collected": False,
            }
        )
        save_state(out, state)
        print(f"batch {batch.id}: {len(chunk)} requests, {batch.processing_status}")


def status(out: Path) -> None:
    api = client()
    for entry in load_state(out):
        batch = api.messages.batches.retrieve(entry["id"])
        counts = batch.request_counts
        print(
            f"{entry['id']} {entry['kind']:6} {batch.processing_status:12}"
            f" processing {counts.processing}, succeeded {counts.succeeded},"
            f" errored {counts.errored}, expired {counts.expired},"
            f" canceled {counts.canceled}; collected {entry['collected']}"
        )


def caption_text(message: Any) -> str:
    parts = [block.text for block in message.content if block.type == "text"]
    return " ".join(" ".join(parts).split()).strip().strip('"').strip()


def collect(out: Path) -> None:
    api = client()
    state = load_state(out)
    results_dir = out / "caption_results"
    results_dir.mkdir(exist_ok=True)
    titles = {photo.pageid: photo.title for photo in photos(out)}
    for entry in state:
        if entry["collected"]:
            continue
        batch = api.messages.batches.retrieve(entry["id"])
        if batch.processing_status != "ended":
            print(f"{entry['id']}: still {batch.processing_status}")
            continue
        done, reviewed = captioned(out)
        raw, captions, errors = [], [], []
        tokens_in = tokens_out = 0
        for result in api.messages.batches.results(entry["id"]):
            pageid = int(result.custom_id.removeprefix("p"))
            record: dict[str, Any] = {"pageid": pageid, "result": result.result.type}
            if result.result.type == "succeeded":
                message = result.result.message
                usage = message.usage
                tokens_in += usage.input_tokens
                tokens_out += usage.output_tokens
                text = caption_text(message)
                words = len(text.split())
                record.update(
                    text=text,
                    stop_reason=message.stop_reason,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
                if message.stop_reason != "end_turn" or not text:
                    title = titles.get(pageid, "")
                    errors.append({**record, "batch": entry["id"], "title": title})
                elif pageid not in done:
                    flag = not CAPTION_WORDS[0] <= words <= CAPTION_WORDS[1]
                    captions.append(
                        {
                            "pageid": pageid,
                            "text": text,
                            "model": MODEL,
                            "focus": "general",
                            "batch": entry["id"],
                            "prompt": PROMPT_VERSION,
                            **({"flag": f"{words} words"} if flag else {}),
                        }
                    )
                    done.add(pageid)
                record["reviewed_before"] = pageid in reviewed
            else:
                errors.append({**record, "batch": entry["id"]})
            raw.append(record)
        append_jsonl(results_dir / f"{entry['id']}.jsonl", raw)
        written = append_jsonl(out / "vlm_captions.jsonl", captions)
        append_jsonl(out / "caption_errors.jsonl", errors)
        cost = tokens_in * PRICE_INPUT + tokens_out * PRICE_OUTPUT
        entry.update(
            collected=True,
            captions=written,
            errors=len(errors),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=round(cost, 4),
        )
        save_state(out, state)
        per = len(raw) or 1
        print(
            f"{entry['id']}: {written} new captions, {len(errors)} errors;"
            f" {tokens_in / per:.0f} in / {tokens_out / per:.0f} out tokens a request;"
            f" US$ {cost:.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="collector's folder")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="count and estimate, without calling the API")
    send = commands.add_parser("submit", help="send a batch")
    which = send.add_mutually_exclusive_group(required=True)
    which.add_argument("--pilot", type=int, help="N/2 reviewed photos and N/2 new ones")
    which.add_argument("--all", action="store_true", help="every uncaptioned photo")
    commands.add_parser("status", help="where each batch is")
    commands.add_parser("collect", help="fetch ended batches and write the captions")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.command == "plan":
        pool, todo, reviewed = training_pool(args.out)
        print(f"training pool: {len(pool)} photos, {len(reviewed)} reviewed by hand")
        estimate(todo)
    elif args.command == "submit":
        _, todo, reviewed = training_pool(args.out)
        # Photos in a batch not collected yet are not sent again; once a batch
        # is collected, its failures are back among the photos to caption.
        pending = {
            pageid
            for entry in load_state(args.out)
            if not entry["collected"]
            for pageid in entry["pageids"]
        }
        todo = [photo for photo in todo if photo.pageid not in pending]
        if args.pilot:
            half = args.pilot // 2
            submit(args.out, reviewed[:half] + todo[: args.pilot - half], "pilot")
        else:
            submit(args.out, todo, "all")
    elif args.command == "status":
        status(args.out)
    elif args.command == "collect":
        collect(args.out)


if __name__ == "__main__":
    main()
