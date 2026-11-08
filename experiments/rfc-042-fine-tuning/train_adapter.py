"""RFC-042 level A: a linear adapter on the query side, trained on CPU.

Section 3.2: one matrix over the query's embedding. The images keep the
vectors they have in the index, so nothing is reindexed. The adapter is
residual and starts at zero: `t' = normalize(t + A t)`, so the untrained
adapter is the current product, query for query. It is trained with the
symmetric contrastive loss of section 3.3 on (training caption, photo) pairs,
against the photos' vectors as the product computes them, and measured on the
validation split of the frozen set of section 5.1.2, against the current
product.

    set PYTHONPATH=
    .venv\\Scripts\\python.exe experiments\\rfc-042-fine-tuning\\train_adapter.py ^
        --out C:\\Users\\me\\rfc042\\commons

**The path is the product's.** Every vector comes from the app's
`ClipEmbeddingModel`: the photos through its preprocessing, the captions and
the queries through `encode_text()`, so through RFC-047's translator, lexicon
and template. A caption in English that the translator would touch is
counted and reported, because it would train the adapter on a mangled text.

**The pairs.** The training captions are the model captions in
`OUT/vlm_captions.jsonl` and the hand captions of `captions.jsonl`, where the
user's reviewed text replaces the model's. A photo in an evaluation series
never trains (the series rule of section 4.3). With `--pairs archive` (or
`both`) they are, or also are, the Portuguese searches `archive_training.py`
wrote for the 2018 photos on D:, three per photo; each epoch draws one
search per photo, so a photo never meets itself in a batch.

**The caches.** Photo and text vectors are kept in `OUT/adapter/`, keyed by
`pageid` and by text (one text cache per version of the lexicon), so a
second run with other hyperparameters encodes nothing again.

**What it reports.** Recall@1 and @5 on validation, in Portuguese through the
translator and in English, for the product (no adapter) and the adapter,
with the paired bootstrap interval of `measure_baseline.py`. With
`--archive`, also the real archive's validation split, in Portuguese
(`measure_archive.py --adapter` breaks it down). The test splits are not
read.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path[:0] = [str(REPOSITORY / "backend")]

from app.domain.entities.image import Image  # noqa: E402
from app.domain.value_objects.device_id import DeviceId  # noqa: E402
from app.domain.value_objects.image_id import ImageId  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.infrastructure.ai.clip_embedding_model import (  # noqa: E402
    TEXT_PROMPT_TEMPLATE,
    ClipEmbeddingModel,
)
from app.infrastructure.ai.device import resolve_device  # noqa: E402
from app.infrastructure.ai.model_runtime import (  # noqa: E402
    CLIP,
    TRANSLATOR,
    default_runtime,
)
from app.infrastructure.ai.query_adapter import lexicon_digest  # noqa: E402
from app.infrastructure.ai.query_translator import (  # noqa: E402
    LEXICON_NAME,
    MarianQueryTranslator,
)

LEXICON = REPOSITORY / "backend" / "app" / "infrastructure" / "ai" / LEXICON_NAME

BATCH = 16
SEED = 42
BOOTSTRAP_ROUNDS = 2000
LOGIT_SCALE = 100.0  # CLIP's learned temperature, the ceiling open_clip clamps it to


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


class VectorCache:
    """Vectors by key in one `.npz`, written back after every new batch."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.vectors: dict[str, np.ndarray] = {}
        if path.exists():
            data = np.load(path, allow_pickle=False)
            for key, row in zip(data["keys"], data["vectors"], strict=True):
                self.vectors[str(key)] = row

    def save(self) -> None:
        keys = sorted(self.vectors)
        temporary = self.path.with_suffix(".tmp.npz")
        np.savez(
            temporary,
            keys=np.asarray(keys),
            vectors=np.stack([self.vectors[k] for k in keys]),
        )
        temporary.replace(self.path)


class Encoder:
    """The product's model, with caches for what it already encoded."""

    def __init__(self, cache_dir: Path) -> None:
        device = resolve_device()
        runtime = default_runtime()
        self.translator = MarianQueryTranslator(
            runtime.loader(TRANSLATOR), device=device
        )
        self.model = ClipEmbeddingModel(
            runtime.loader(CLIP), translator=self.translator, device=device
        )
        cache_dir.mkdir(exist_ok=True)
        self.images = VectorCache(cache_dir / "image_vectors.npz")
        # A text's vector depends on the lexicon too, so a lexicon edit
        # starts a new text cache instead of reusing stale vectors.
        version = hashlib.sha256(LEXICON.read_bytes()).hexdigest()[:12]
        self.texts = VectorCache(cache_dir / f"text_vectors-{version}.npz")
        self.too_long: dict[str, str] = {}

    def image_vectors(self, items: Sequence[tuple[int | str, Path]]) -> np.ndarray:
        """By `pageid` for the Commons photos, by `coco:<id>` for COCO's."""
        missing = [(p, path) for p, path in items if str(p) not in self.images.vectors]
        started = time.perf_counter()
        for start in range(0, len(missing), BATCH):
            chunk = missing[start : start + BATCH]
            images = [
                Image(
                    id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
                    device_id=DeviceId(uuid.UUID(int=0)),
                    relative_path=ImagePath(str(path)),
                    absolute_path=ImagePath(str(path)),
                    filename=path.stem,
                    extension=path.suffix.lstrip(".").lower(),
                )
                for _, path in chunk
            ]
            contents = [path.read_bytes() for _, path in chunk]
            for (pageid, _), vector in zip(
                chunk, self.model.encode_images(images, contents), strict=True
            ):
                self.images.vectors[str(pageid)] = np.asarray(vector.values, np.float32)
            if (start // BATCH) % 25 == 24:
                self.images.save()
                done = start + len(chunk)
                seconds = time.perf_counter() - started
                print(f"    images {done}/{len(missing)}, {seconds:.0f} s")
        if missing:
            self.images.save()
        return np.stack([self.images.vectors[str(p)] for p, _ in items])

    def encode_texts(self, texts: Sequence[str]) -> None:
        """Encode and cache what is not cached yet, saving every 250 texts.

        A prompt longer than CLIP's 77 tokens makes the product's
        `encode_text()` fail (a long translation, or Marian repeating itself);
        such a text is kept in `too_long`, with its prompt, and not cached.
        """
        missing = [
            t
            for t in dict.fromkeys(texts)
            if t not in self.texts.vectors and t not in self.too_long
        ]
        started = time.perf_counter()
        for count, text in enumerate(missing, start=1):
            try:
                vector = self.model.encode_text(text).values
            except ValueError as error:
                if "max_position_embeddings" not in str(error):
                    raise
                self.too_long[text] = self.model.build_prompt(text)
                continue
            self.texts.vectors[text] = np.asarray(vector, np.float32)
            if count % 250 == 0:
                self.texts.save()
                seconds = time.perf_counter() - started
                print(f"    texts {count}/{len(missing)}, {seconds:.0f} s")
        if missing:
            self.texts.save()

    def text_vectors(self, texts: Sequence[str]) -> np.ndarray:
        """One row per text; a text the product cannot encode stops the run."""
        self.encode_texts(texts)
        failed = [t for t in texts if t in self.too_long]
        if failed:
            raise SystemExit(f"too long for CLIP: {failed[0]!r} ({len(failed)})")
        return np.stack([self.texts.vectors[t] for t in texts])


def training_pairs(out: Path) -> list[tuple[int, Path, str, str]]:
    """(pageid, photo, caption, source): reviewed text over the model's text."""
    import sqlite3

    evaluation = {r["series"] for r in read_jsonl(HERE / "eval_set.jsonl")}
    db = sqlite3.connect(
        f"file:{(out / 'collection.sqlite').as_posix()}?mode=ro", uri=True
    )
    sys.path.insert(0, str(HERE))
    import collect_commons

    where = {
        int(pageid): (out / str(path), collect_commons.series_key(json.loads(info)))
        for pageid, path, info in db.execute(
            "SELECT pageid, path, info FROM files"
            " WHERE downloaded_at IS NOT NULL AND decision = 'accepted'"
        )
    }
    text: dict[int, tuple[str, str]] = {}
    for record in read_jsonl(out / "vlm_captions.jsonl"):
        text[int(record["pageid"])] = (str(record["text"]), "model")
    for record in read_jsonl(HERE / "captions.jsonl"):
        if record.get("kind") == "review" and record.get("text"):
            text[int(record["pageid"])] = (str(record["text"]), "reviewed")
        elif record.get("kind") == "caption" and record.get("text"):
            text[int(record["pageid"])] = (str(record["text"]), "hand")
    pairs = []
    for pageid, (caption, source) in sorted(text.items()):
        if pageid in where and where[pageid][1] not in evaluation:
            pairs.append((pageid, where[pageid][0], caption, source))
    return pairs


def archive_pairs(training: Path) -> list[tuple[int, Path, str, str]]:
    """(id, photo, search, "archive"): `archive_training.py`'s searches.

    Every search of a photo is a pair; the last answer per photo counts, and
    a photo answered PULAR (not taken from the air) has none.
    """
    answers: dict[int, dict[str, Any]] = {}
    for record in read_jsonl(training / "training_queries.jsonl"):
        answers[int(record["id"])] = record
    where = {
        int(r["id"]): training / str(r["path"])
        for r in read_jsonl(training / "training_photos.jsonl")
    }
    pairs = []
    for photo_id, record in sorted(answers.items()):
        if record.get("skip") or photo_id not in where:
            continue
        for query in record["queries"]:
            pairs.append((photo_id, where[photo_id], str(query), "archive"))
    return pairs


def adapt(texts: torch.Tensor, matrix: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(texts + texts @ matrix.T, dim=-1)


def train(
    texts: np.ndarray,
    images: np.ndarray,
    photos: Sequence[int],
    epochs: int,
    lr: float,
    weight_decay: float,
    batch: int,
) -> tuple[torch.Tensor, list[float]]:
    """`photos[i]` is the photo of pair i. Each epoch draws one pair per photo:
    a photo with several searches would otherwise meet itself in a batch, and
    its own searches would count as negatives."""
    torch.manual_seed(SEED)
    t = torch.from_numpy(texts)
    v = torch.from_numpy(images)
    by_photo: dict[int, list[int]] = {}
    for position, photo in enumerate(photos):
        by_photo.setdefault(photo, []).append(position)
    members = list(by_photo.values())
    matrix = torch.zeros(t.shape[1], t.shape[1], requires_grad=True)
    optimizer = torch.optim.Adam([matrix], lr=lr)
    losses = []
    generator = torch.Generator().manual_seed(SEED)
    for _ in range(epochs):
        drawn = torch.tensor(
            [
                group[int(torch.randint(len(group), (1,), generator=generator))]
                for group in members
            ]
        )
        order = drawn[torch.randperm(len(drawn), generator=generator)]
        total = 0.0
        for start in range(0, len(order), batch):
            index = order[start : start + batch]
            if len(index) < 2:
                continue
            logits = LOGIT_SCALE * adapt(t[index], matrix) @ v[index].T
            target = torch.arange(len(index))
            loss = (
                torch.nn.functional.cross_entropy(logits, target)
                + torch.nn.functional.cross_entropy(logits.T, target)
            ) / 2 + weight_decay * matrix.pow(2).sum()
            optimizer.zero_grad()
            loss.backward()  # type: ignore[no-untyped-call]
            optimizer.step()
            total += float(loss) * len(index)
        losses.append(total / len(order))
    return matrix.detach(), losses


def ranks(texts: np.ndarray, gallery: np.ndarray, targets: Sequence[int]) -> np.ndarray:
    scores = texts @ gallery.T
    own = scores[np.arange(len(targets)), targets]
    above: np.ndarray = (scores > own[:, None]).sum(axis=1)
    tied: np.ndarray = (scores == own[:, None]).sum(axis=1) - 1
    result: np.ndarray = 1 + above + tied
    return result


def paired_interval(
    mine: np.ndarray, base: np.ndarray, k: int
) -> tuple[float, float, float]:
    hits = (mine <= k).astype(float) - (base <= k).astype(float)
    rng = np.random.default_rng(SEED)
    draws = rng.integers(0, len(hits), size=(BOOTSTRAP_ROUNDS, len(hits)))
    means = hits[draws].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return float(hits.mean()), float(low), float(high)


def compare(
    label: str,
    vectors: np.ndarray,
    gallery: np.ndarray,
    targets: Sequence[int],
    matrix: torch.Tensor,
) -> dict[str, Any]:
    """Recall of the product and of the adapter on the same queries, printed."""
    base = ranks(vectors, gallery, targets)
    adapted = adapt(torch.from_numpy(vectors), matrix).numpy()
    mine = ranks(adapted, gallery, targets)
    line = f"  {label}:"
    entry: dict[str, Any] = {}
    for k in (1, 5, 10):
        before = float((base <= k).mean())
        after = float((mine <= k).mean())
        entry[f"R@{k}"] = {"product": before, "adapter": after}
        line += f"  R@{k} {100 * before:.1f} -> {100 * after:.1f}"
        if k in (1, 5):
            diff, low, high = paired_interval(mine, base, k)
            entry[f"R@{k}"]["interval"] = [diff, low, high]
            line += f" ({100 * diff:+.1f} [{100 * low:+.1f}, {100 * high:+.1f}])"
    entry["ranks"] = mine.tolist()
    print(line)
    return entry


def regression(encoder: Encoder, coco: Path, matrix: torch.Tensor) -> dict[str, Any]:
    """Section 5.1.3: COCO's captions, five per photo, against its photos."""
    photos = list(read_jsonl(coco / "coco_regression.jsonl"))
    gallery = encoder.image_vectors(
        [(f"coco:{p['image_id']}", coco / p["path"]) for p in photos]
    )
    captions = [c for p in photos for c in p["captions"]]
    targets = [i for i, p in enumerate(photos) for _ in p["captions"]]
    touched = sum(
        encoder.model.build_prompt(c) != TEXT_PROMPT_TEMPLATE.format(query=c)
        for c in captions
    )
    print(f"\nregression, COCO ({len(captions)} captions x {len(photos)} photos), in %")
    print(f"  captions the translator would rewrite: {touched}")
    entry = compare("en", encoder.text_vectors(captions), gallery, targets, matrix)
    entry["touched_captions"] = touched
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="the collector's folder"
    )
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-3)
    parser.add_argument("--batch", type=int, default=256)
    parser.add_argument("--tag", default="", help="a name for this run's files")
    parser.add_argument(
        "--coco", type=Path, help="the folder of fetch_coco.py, for section 5.1.3"
    )
    parser.add_argument(
        "--pairs",
        choices=("commons", "archive", "both"),
        default="commons",
        help="train on the Commons captions, the archive's searches, or both",
    )
    parser.add_argument(
        "--training", type=Path, help="archive_training.py's folder (--pairs archive)"
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="sample_archive.py's folder: also report its validation split",
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.pairs != "commons" and args.training is None:
        raise SystemExit(f"--pairs {args.pairs} needs --training")

    cache_dir = args.out / "adapter"
    encoder = Encoder(cache_dir)
    pairs = []
    if args.pairs in ("commons", "both"):
        pairs += training_pairs(args.out)
    if args.pairs in ("archive", "both"):
        pairs += archive_pairs(args.training)
    sources: dict[str, int] = {}
    for *_, source in pairs:
        sources[source] = sources.get(source, 0) + 1
    print(f"training pairs: {len(pairs)} {sources}")

    # English captions only: the archive's searches are Portuguese, and are
    # meant to go through the translator.
    touched = [
        caption
        for *_, caption, source in pairs
        if source != "archive"
        and encoder.model.build_prompt(caption)
        != TEXT_PROMPT_TEMPLATE.format(query=caption)
    ]
    print(f"captions the translator would rewrite: {len(touched)}")
    for caption in touched[:5]:
        print(f"    {caption}")

    started = time.perf_counter()
    encoder.encode_texts([caption for *_, caption, _ in pairs])
    dropped = [pair for pair in pairs if pair[2] in encoder.too_long]
    if dropped:
        print(f"pairs too long for CLIP after translation, left out: {len(dropped)}")
        for _, _, caption, _ in dropped[:5]:
            print(f"    {caption!r} -> {encoder.too_long[caption][:160]!r}")
        pairs = [pair for pair in pairs if pair[2] not in encoder.too_long]
    images = encoder.image_vectors([(p, path) for p, path, *_ in pairs])
    texts = encoder.text_vectors([caption for *_, caption, _ in pairs])
    print(f"training vectors ready in {time.perf_counter() - started:.0f} s")

    rows = list(read_jsonl(HERE / "eval_set.jsonl"))
    gallery = encoder.image_vectors([(r["pageid"], args.out / r["path"]) for r in rows])
    chosen = [i for i, r in enumerate(rows) if r["split"] == "validation"]
    queries = {
        "pt": encoder.text_vectors([rows[i]["pt"] for i in chosen]),
        "en": encoder.text_vectors([rows[i]["en"] for i in chosen]),
    }
    if args.archive:
        archive_rows = list(read_jsonl(args.archive / "archive_eval_set.jsonl"))
        archive_gallery = encoder.image_vectors(
            [(r["pageid"], args.archive / r["path"]) for r in archive_rows]
        )
        archive_chosen = [
            i for i, r in enumerate(archive_rows) if r["split"] == "validation"
        ]
        archive_queries = encoder.text_vectors(
            [archive_rows[i]["pt"] for i in archive_chosen]
        )

    started = time.perf_counter()
    photos = [photo for photo, *_ in pairs]
    matrix, losses = train(
        texts, images, photos, args.epochs, args.lr, args.weight_decay, args.batch
    )
    seconds = time.perf_counter() - started
    print(f"trained in {seconds:.1f} s; loss {losses[0]:.3f} -> {losses[-1]:.3f}")
    print(f"  ||A|| = {float(matrix.norm()):.3f}")

    report: dict[str, Any] = {
        "when": datetime.now(UTC).isoformat(),
        "pairs": len(pairs),
        "sources": sources,
        "touched_captions": len(touched),
        "lexicon_sha256": lexicon_digest(LEXICON.read_bytes()),
        "too_long": {pair[2]: encoder.too_long[pair[2]] for pair in dropped},
        "hyper": {
            "epochs": args.epochs,
            "lr": args.lr,
            "weight_decay": args.weight_decay,
            "batch": args.batch,
            "logit_scale": LOGIT_SCALE,
        },
        "losses": losses,
        "train_seconds": seconds,
        "validation": {},
    }
    print(f"\nvalidation ({len(chosen)} queries x {len(rows)} photos), in %")
    for language, vectors in queries.items():
        report["validation"][language] = compare(
            language, vectors, gallery, chosen, matrix
        )
    if args.archive:
        size = f"{len(archive_chosen)} queries x {len(archive_rows)} photos"
        print(f"\nreal archive, validation ({size}), in %")
        report["archive_validation"] = compare(
            "pt", archive_queries, archive_gallery, archive_chosen, matrix
        )

    if args.coco:
        report["regression"] = regression(encoder, args.coco, matrix)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"adapter-{args.tag + '-' if args.tag else ''}{stamp}"
    np.save(cache_dir / f"{name}.npy", matrix.numpy())
    (cache_dir / f"{name}.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8"
    )
    print(f"\nwrote {cache_dir / name}.npy and .json")


if __name__ == "__main__":
    main()
