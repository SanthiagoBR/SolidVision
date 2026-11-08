"""Training searches for RFC-042's level A, from the 2018 drone photos on D:.

The person moved every 2018 photo to D: to be training data. The real-archive
evaluation set (`sample_archive.py`, `freeze_archive_set.py`) has no day of
2018, so the two never share a flight. Level A adapts the query's vector, so
what it trains on should look like a query: for each photo Claude writes
three searches in Brazilian Portuguese (short, medium and long) as the person
would type them. They reach CLIP through the product's translator, as a
typed query does (`train_adapter.py`).

    set PY=C:\\Users\\me\\rfc042\\venv-api\\Scripts\\python.exe
    %PY% archive_training.py --out C:\\Users\\me\\rfc042\\training prepare --source D:/
    %PY% archive_training.py --out ... plan
    %PY% archive_training.py --out ... submit --pilot 24
    %PY% archive_training.py --out ... status
    %PY% archive_training.py --out ... collect
    %PY% archive_training.py --out ... submit --all

**prepare.** Every JPEG under `--source` whose camera is a drone's, by EXIF,
gets a 768 px thumbnail in `OUT/images/`. It also gets a line in
`OUT/training_photos.jsonl`: its id (`8_000_000_000 + n`, in path order),
the original path, the day, the camera and the position. System folders are
skipped, and a rerun skips the photos already there.

**The prompt.** The vocabulary it suggests is the region's: the plain words
a person in Santa Catarina types. Its example is invented, and the
evaluation set is not read (section 4.6).

**What is kept.** As in `caption_batch.py`:

- each batch's raw results go to `OUT/query_results/`;
- the batches go to `OUT/query_batches.json`, written right after each one
  is created;
- the searches go to `OUT/training_queries.jsonl`, with the model, the batch
  and the prompt version.

A photo answered PULAR (not taken from the air) is recorded with
`skip: true`. Answers that are not three lines go to
`OUT/query_errors.jsonl`, and those photos are sent again by the next
`submit`.

The API key is read as in `caption_batch.py`, and is never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from caption_batch import (  # noqa: E402
    PRICE_INPUT,
    PRICE_OUTPUT,
    append_jsonl,
    client,
    read_jsonl,
)
from collect_commons import spread  # noqa: E402

MODEL = "claude-sonnet-5-5"
EFFORT = "low"
MAX_TOKENS = 2048  # thinking counts toward it; three searches are ~60 tokens
PROMPT_VERSION = "q2"
# q1 (the pilot, 24 photos): searches of 4, 11 and 18 words at the median,
# about half again the lengths asked, with the longest at 27 and "ao fundo"
# where the person writes "no fundo". q2 asks for shorter ones, firmly.
BATCH_REQUESTS = 1000  # ~110 MB of images per batch; the API allows 256 MB
THUMBNAIL = 768
ID_BASE = 8_000_000_000
SKIP_WORD = "PULAR"
DRONE_MAKERS = ("dji", "hasselblad")
PHOTOS = "training_photos.jsonl"
QUERIES = "training_queries.jsonl"

SYSTEM = """\
You write searches in Brazilian Portuguese for photos a drone took in Santa \
Catarina, in the south of Brazil: houses and their yards, sheds, small \
businesses, farms, rice fields, banana plantations, fish ponds, roads and \
small towns, often one property seen from close by. The person who took them \
finds a photo in their archive by typing a few plain words, and your searches \
teach the search engine which words find which photo.

Write three searches this person could type to find this photo, one per \
line, without numbers:
- the first of two to four words: the main subject;
- the second of five to eight words: the main subject and one or two things \
beside it;
- the third of nine to twelve words: the few things that stand out most.

Keep to these lengths, counting every word: people type short searches, and \
even the third one is never longer than twelve words. Leave out small \
details.

Write as a person types, not as a caption. Start with the main subject \
("Casa com...", "Galpão azul...", "Plantação de arroz..."), use everyday \
words (casa, galpão, pátio, pasto, mato, morro, bananal, plantação de arroz, \
tanque de peixes, estrada de terra, rodovia), and give the color of roofs, \
walls and vehicles when it is clear ("telhado laranja", "carro branco"). Say \
"no fundo" (never "ao fundo") for what is far behind and "do lado" or "ao \
lado" for what is next to it; people do not type "à direita" or "à \
esquerda".

- Describe only what the photo shows; when unsure what something is, use a \
plainer word.
- Never name places, businesses, brands or people, and do not guess what a \
building is used for unless it is plain to see.
- Do not start with "Vista aérea", "Foto de" or "Imagem de": every photo is \
from a drone.
- Count only up to three; for more, say "vários" or "várias".
- No quotes and no final period.

If the photo was not taken from the air (it was taken on the ground, or shows \
only the sky, or is blurred or dark), answer with the single word PULAR.

An example answer, for another photo:
Ginásio de esportes
Ginásio com telhado curvo e estacionamento
Ginásio de esportes com telhado curvo, estacionamento cheio e casas no fundo"""
PROMPT_SHA = hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest()[:12]
ASK = "Escreva as três buscas para esta foto."


def prepare(out: Path, source: Path) -> None:
    import sample_archive
    from PIL import Image, ImageOps

    manifest = out / PHOTOS
    existing = list(read_jsonl(manifest))
    known = {str(r["source"]) for r in existing}
    next_id = max((int(r["id"]) for r in existing), default=ID_BASE) + 1
    entries = sorted(
        sample_archive.walk(source, skip_system=True),
        key=lambda entry: entry.path.casefold(),
    )
    print(f"{len(entries)} JPEGs under {source}; {len(known)} already prepared")
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    not_drone: Counter[str] = Counter()
    added = 0
    for entry in entries:
        path = Path(entry.path)
        relative = str(path.relative_to(source))
        if relative in known:
            continue
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            exif = sample_archive.read_exif(image)
            if not exif["make"].casefold().startswith(DRONE_MAKERS):
                not_drone[f"{exif['make']} {exif['model']}".strip() or "?"] += 1
                continue
            image.draft("RGB", (THUMBNAIL, THUMBNAIL))
            small = ImageOps.exif_transpose(image).convert("RGB")
            small.thumbnail((THUMBNAIL, THUMBNAIL))
            target = images / f"{next_id}.jpg"
            small.save(target, "JPEG", quality=85)
        record = {
            "id": next_id,
            "path": f"images/{next_id}.jpg",
            "source": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "day": exif["day"],
            "taken": exif["taken"],
            "camera": f"{exif['make']} {exif['model']}".strip(),
            "gps": exif["gps"],
        }
        append_jsonl(manifest, [record])
        next_id += 1
        added += 1
        if added % 200 == 0:
            print(f"  {added} prepared")
    print(f"prepared {added}; not from a drone: {dict(not_drone)}")


def load_state(out: Path) -> list[dict[str, Any]]:
    path = out / "query_batches.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_state(out: Path, state: list[dict[str, Any]]) -> None:
    path = out / "query_batches.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=1), encoding="utf-8")
    temporary.replace(path)


def to_send(out: Path) -> list[dict[str, Any]]:
    """Photos without an answer to this prompt version and not in a batch still
    out, in `spread` order: a photo answered with an older prompt goes again,
    so that every pair is written with the same prompt."""
    answered = {
        int(r["id"])
        for r in read_jsonl(out / QUERIES)
        if r.get("prompt") == PROMPT_VERSION
    }
    out_now = {
        int(photo_id)
        for entry in load_state(out)
        if not entry["collected"]
        for photo_id in entry["ids"]
    }
    photos = [
        photo
        for photo in read_jsonl(out / PHOTOS)
        if int(photo["id"]) not in answered | out_now
    ]
    photos.sort(key=lambda photo: spread(int(photo["id"])))
    return photos


def estimate(out: Path, photos: list[dict[str, Any]]) -> None:
    """Tokens and cost without calling the API: images at (w*h)/750 tokens."""
    from PIL import Image

    image_tokens = 0
    for photo in photos:
        with Image.open(out / photo["path"]) as image:
            image_tokens += image.size[0] * image.size[1] // 750
    text_tokens = (len(SYSTEM) // 4 + 20) * len(photos)  # about 4 characters a token
    tokens_in = image_tokens + text_tokens
    print(f"to send: {len(photos)} photos, about {tokens_in / 1e6:.2f} M input tokens")
    for label, per_answer in (("answers only", 60), ("with some thinking", 300)):
        tokens_out = per_answer * len(photos)
        cost = tokens_in * PRICE_INPUT + tokens_out * PRICE_OUTPUT
        print(f"  {label:20} ~{tokens_out / 1e6:.2f} M output, about US$ {cost:.2f}")


def request_params(out: Path, photo: dict[str, Any]) -> dict[str, Any]:
    data = (out / photo["path"]).read_bytes()
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
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(data).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": ASK},
                ],
            }
        ],
    }


def submit(out: Path, chosen: list[dict[str, Any]], kind: str) -> None:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    api = client()
    state = load_state(out)
    for start in range(0, len(chosen), BATCH_REQUESTS):
        chunk = chosen[start : start + BATCH_REQUESTS]
        requests = [
            Request(
                custom_id=f"t{photo['id']}",
                params=MessageCreateParamsNonStreaming(**request_params(out, photo)),  # type: ignore[typeddict-item]
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
                "ids": [int(photo["id"]) for photo in chunk],
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
            f" errored {counts.errored}, expired {counts.expired};"
            f" collected {entry['collected']}"
        )


def searches(text: str) -> list[str] | None:
    """The three searches; [] for PULAR; None when the answer is not that."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*\u2022])\s*", "", line)
        line = " ".join(line.split()).strip("\"'\u201c\u201d").rstrip(".").strip()
        if line:
            lines.append(line)
    if len(lines) == 1 and lines[0].upper() == SKIP_WORD:
        return []
    return lines if len(lines) == 3 else None


def collect(out: Path) -> None:
    api = client()
    state = load_state(out)
    results_dir = out / "query_results"
    results_dir.mkdir(exist_ok=True)
    for entry in state:
        if entry["collected"]:
            continue
        batch = api.messages.batches.retrieve(entry["id"])
        if batch.processing_status != "ended":
            print(f"{entry['id']}: still {batch.processing_status}")
            continue
        raw, answers, errors = [], [], []
        tokens_in = tokens_out = 0
        for result in api.messages.batches.results(entry["id"]):
            photo_id = int(result.custom_id.removeprefix("t"))
            record: dict[str, Any] = {"id": photo_id, "result": result.result.type}
            if result.result.type == "succeeded":
                message = result.result.message
                tokens_in += message.usage.input_tokens
                tokens_out += message.usage.output_tokens
                text = "\n".join(b.text for b in message.content if b.type == "text")
                record.update(
                    text=text,
                    stop_reason=message.stop_reason,
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                )
                found = searches(text) if message.stop_reason == "end_turn" else None
                if found is None:
                    errors.append({**record, "batch": entry["id"]})
                else:
                    answers.append(
                        {
                            "id": photo_id,
                            "queries": found,
                            **({"skip": True} if not found else {}),
                            "model": MODEL,
                            "batch": entry["id"],
                            "prompt": PROMPT_VERSION,
                        }
                    )
            else:
                errors.append({**record, "batch": entry["id"]})
            raw.append(record)
        append_jsonl(results_dir / f"{entry['id']}.jsonl", raw)
        append_jsonl(out / QUERIES, answers)
        append_jsonl(out / "query_errors.jsonl", errors)
        cost = tokens_in * PRICE_INPUT + tokens_out * PRICE_OUTPUT
        skipped = sum(1 for a in answers if a.get("skip"))
        entry.update(
            collected=True,
            answers=len(answers),
            skipped=skipped,
            errors=len(errors),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=round(cost, 4),
        )
        save_state(out, state)
        per = len(raw) or 1
        print(
            f"{entry['id']}: {len(answers) - skipped} photos with searches,"
            f" {skipped} PULAR, {len(errors)} errors;"
            f" {tokens_in / per:.0f} in / {tokens_out / per:.0f} out tokens a"
            f" request; US$ {cost:.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="the training folder")
    commands = parser.add_subparsers(dest="command", required=True)
    ready = commands.add_parser("prepare", help="thumbnails and the photo list")
    ready.add_argument("--source", type=Path, required=True, help="e.g. D:/")
    commands.add_parser("plan", help="count and estimate, without calling the API")
    send = commands.add_parser("submit", help="send a batch")
    which = send.add_mutually_exclusive_group(required=True)
    which.add_argument("--pilot", type=int, help="the first N photos to send")
    which.add_argument("--all", action="store_true", help="every photo left")
    commands.add_parser("status", help="where each batch is")
    commands.add_parser("collect", help="fetch ended batches and write the searches")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.command == "prepare":
        prepare(args.out, args.source)
    elif args.command == "plan":
        photos = list(read_jsonl(args.out / PHOTOS))
        print(f"{len(photos)} photos prepared")
        estimate(args.out, to_send(args.out))
    elif args.command == "submit":
        chosen = to_send(args.out)
        if args.pilot:
            submit(args.out, chosen[: args.pilot], "pilot")
        else:
            submit(args.out, chosen, "all")
    elif args.command == "status":
        status(args.out)
    elif args.command == "collect":
        collect(args.out)


if __name__ == "__main__":
    main()
