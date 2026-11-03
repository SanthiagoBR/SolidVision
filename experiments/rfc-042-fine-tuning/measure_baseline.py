"""Measure CLIP checkpoints on the frozen evaluation set, before any training.

RFC-042 section 3.1: before anything is trained, the checkpoints already
adapted to aerial imagery are measured against the current one. If one beats
it by more than the noise, the path becomes swapping the checkpoint, and our
own training continues only if a gap remains.

    set PYTHONPATH=C:\\Users\\me\\rfc042\\pylib
    .venv\\Scripts\\python.exe experiments\\rfc-042-fine-tuning\\measure_baseline.py ^
        --out C:\\Users\\me\\rfc042\\commons --models C:\\Users\\me\\rfc042\\models

Runs on the app's own environment, which has `torch` and `transformers`;
`open_clip` (for RemoteCLIP and GeoRSCLIP) comes from a separate folder on
`PYTHONPATH`, so the app's environment is not touched.

**What is measured.**

- The current checkpoint exactly as the product uses it: the app's
  `ClipEmbeddingModel`, loaded from the lockfile's tree, with its image
  preprocessing, its `a photo of {query}` template and its translator.
- RemoteCLIP and GeoRSCLIP ViT-B/32, through `open_clip`, with each one's
  own preprocessing. Their text goes through the same translator, and is
  tried with the product's template and with none, the form their authors
  trained on. Both activations (GELU, QuickGELU) are tried, because the
  authors' instructions disagree on which architecture name to load.
- Section 5.1.2: each query of the split against all 525 evaluation photos,
  in English as written (`en`), in Portuguese through the translator
  (`pt`), and the southern variants through the translator (`sul`, the
  subset that has one, next to `pt` on the same subset).
- Section 5.1.1: RFC-022's 25 queries and 45 images, strict top-1 and
  pairwise, as the RFC-023 bake-off reported them. The current checkpoint
  must reproduce 60.0% and 80.4% there, which checks this script.

The metrics are recall@1, @5 and @10 (the photo is among the first K) and
the mean reciprocal rank. The difference from the current checkpoint comes
with a 95% interval from a paired bootstrap over queries: a candidate beats
the current one by more than the noise when the whole interval is above zero.

**The test split.** Section 4.6: opened only for the final evaluation.
`--split test` needs `--open-test`, and the first opening is recorded next
to the results.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image as PILImage

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
from app.infrastructure.ai.query_translator import (  # noqa: E402
    MarianQueryTranslator,
    QueryTranslator,
)

DEMO = REPOSITORY / "backend" / "dataset" / "demo"
BATCH = 16
BOOTSTRAP_ROUNDS = 2000
SEED = 42
RAW_TEMPLATE = "{query}"

# (name, checkpoint relative to --models, open_clip architecture). The
# commits and SHA-256 of the files are in the RFC's record of this run.
OPEN_CLIP_CANDIDATES = (
    ("RemoteCLIP B/32 (GELU)", "RemoteCLIP/RemoteCLIP-ViT-B-32.pt", "ViT-B-32"),
    (
        "RemoteCLIP B/32 (QuickGELU)",
        "RemoteCLIP/RemoteCLIP-ViT-B-32.pt",
        "ViT-B-32-quickgelu",
    ),
    ("GeoRSCLIP B/32 (GELU)", "GeoRSCLIP/ckpt/RS5M_ViT-B-32.pt", "ViT-B-32"),
    (
        "GeoRSCLIP B/32 (QuickGELU)",
        "GeoRSCLIP/ckpt/RS5M_ViT-B-32.pt",
        "ViT-B-32-quickgelu",
    ),
)
CURRENT = "atual (LAION B/32)"


class CachingTranslator(QueryTranslator):
    """The product's translator, asked once per distinct query.

    Every candidate reads the same English, so the comparison is between
    image-text models and not between translation runs. Greedy decoding
    makes the cache invisible: the same query always gets the same English.
    """

    def __init__(self, inner: QueryTranslator) -> None:
        self._inner = inner
        self.cache: dict[str, str] = {}
        self.seconds = 0.0

    def to_english(self, text: str) -> str:
        if text not in self.cache:
            started = time.perf_counter()
            self.cache[text] = self._inner.to_english(text)
            self.seconds += time.perf_counter() - started
        return self.cache[text]


@dataclass
class Candidate:
    name: str
    templates: tuple[str, ...]
    encode_images: Callable[[Sequence[Path]], np.ndarray]
    encode_texts: Callable[[Sequence[str], str], np.ndarray]
    """Queries (as typed) and a template; translation happens inside."""
    notes: list[str] = field(default_factory=list)


def unit_rows(matrix: torch.Tensor) -> np.ndarray:
    return torch.nn.functional.normalize(matrix.float(), dim=-1).cpu().numpy()


def current_candidate(translator: CachingTranslator, device: torch.device) -> Candidate:
    runtime = default_runtime()
    model = ClipEmbeddingModel(
        runtime.loader(CLIP), translator=translator, device=device
    )

    def encode_images(paths: Sequence[Path]) -> np.ndarray:
        rows: list[list[float]] = []
        for start in range(0, len(paths), BATCH):
            chunk = paths[start : start + BATCH]
            images = [
                Image(
                    id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
                    device_id=DeviceId(uuid.UUID(int=0)),
                    relative_path=ImagePath(str(path)),
                    absolute_path=ImagePath(str(path)),
                    filename=path.stem,
                    extension=path.suffix.lstrip(".").lower(),
                )
                for path in chunk
            ]
            contents = [path.read_bytes() for path in chunk]
            rows.extend(
                list(vector.values) for vector in model.encode_images(images, contents)
            )
        return np.asarray(rows, dtype=np.float32)

    def encode_texts(queries: Sequence[str], template: str) -> np.ndarray:
        if template != TEXT_PROMPT_TEMPLATE:
            raise ValueError(
                "the current checkpoint is measured with the product's template only"
            )
        return np.asarray(
            [list(model.encode_text(query).values) for query in queries],
            dtype=np.float32,
        )

    return Candidate(CURRENT, (TEXT_PROMPT_TEMPLATE,), encode_images, encode_texts)


def open_clip_candidate(
    name: str,
    checkpoint: Path,
    architecture: str,
    translator: CachingTranslator,
    device: torch.device,
) -> Candidate:
    import open_clip

    model, _, preprocess = open_clip.create_model_and_transforms(architecture)
    # weights_only: a state dict of tensors loads; anything that would run
    # code while unpickling is refused instead.
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    state = {key.removeprefix("module."): value for key, value in state.items()}
    result = model.load_state_dict(state, strict=False)
    if result.missing_keys:
        missing = result.missing_keys
        raise SystemExit(f"{name}: {len(missing)} weights missing, e.g. {missing[:3]}")
    notes = (
        [f"unexpected keys ignored: {result.unexpected_keys}"]
        if result.unexpected_keys
        else []
    )
    model = model.to(device).eval()
    tokenizer = open_clip.get_tokenizer(architecture)

    def encode_images(paths: Sequence[Path]) -> np.ndarray:
        rows = []
        for start in range(0, len(paths), BATCH):
            chunk = paths[start : start + BATCH]
            batch = torch.stack(
                [
                    preprocess(PILImage.open(io.BytesIO(p.read_bytes())).convert("RGB"))
                    for p in chunk
                ]
            )
            with torch.no_grad():
                rows.append(unit_rows(model.encode_image(batch.to(device))))
        return np.concatenate(rows)

    def encode_texts(queries: Sequence[str], template: str) -> np.ndarray:
        prompts = [
            template.format(query=translator.to_english(query)) for query in queries
        ]
        rows = []
        for start in range(0, len(prompts), 64):
            with torch.no_grad():
                rows.append(
                    unit_rows(
                        model.encode_text(
                            tokenizer(prompts[start : start + 64]).to(device)
                        )
                    )
                )
        return np.concatenate(rows)

    return Candidate(
        name, (TEXT_PROMPT_TEMPLATE, RAW_TEMPLATE), encode_images, encode_texts, notes
    )


def ranks(texts: np.ndarray, gallery: np.ndarray, targets: Sequence[int]) -> np.ndarray:
    """1-based rank of each query's photo among the gallery (ties count against it)."""
    scores = texts @ gallery.T
    own = scores[np.arange(len(targets)), targets]
    above: np.ndarray = (scores > own[:, None]).sum(axis=1)
    tied: np.ndarray = (scores == own[:, None]).sum(axis=1) - 1
    result: np.ndarray = 1 + above + tied
    return result


def summary(rank: np.ndarray) -> dict[str, float]:
    return {
        "R@1": float((rank <= 1).mean()),
        "R@5": float((rank <= 5).mean()),
        "R@10": float((rank <= 10).mean()),
        "MRR": float((1.0 / rank).mean()),
        "n": int(len(rank)),
    }


def paired_interval(
    candidate: np.ndarray, current: np.ndarray, k: int
) -> tuple[float, float, float]:
    """Difference in recall@k and its 95% bootstrap interval, resampling queries."""
    hits = (candidate <= k).astype(float) - (current <= k).astype(float)
    rng = np.random.default_rng(SEED)
    draws = rng.integers(0, len(hits), size=(BOOTSTRAP_ROUNDS, len(hits)))
    means = hits[draws].mean(axis=1)
    return (
        float(hits.mean()),
        float(np.percentile(means, 2.5)),
        float(np.percentile(means, 97.5)),
    )


def demo_benchmark(candidate: Candidate, template: str) -> dict[str, float]:
    """RFC-022's corpus as RFC-023 scored it: strict top-1 and pairwise, English."""
    manifest = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))
    queries = json.loads((DEMO / "queries.json").read_text(encoding="utf-8"))["queries"]
    paths = [entry["relative_path"] for entry in manifest["images"]]
    index = {path: i for i, path in enumerate(paths)}
    gallery = candidate.encode_images([DEMO / path for path in paths])
    texts = candidate.encode_texts([q["query"] for q in queries], template)
    scores = texts @ gallery.T
    strict = pairs = right = 0
    for row, query in zip(scores, queries, strict=True):
        strict += paths[int(row.argmax())] in query["relevant"]
        for relevant in query["relevant"]:
            for negative in query["hard_negatives"]:
                pairs += 1
                right += row[index[relevant]] > row[index[negative]]
    return {"strict-EN": strict / len(queries), "pairwise-EN": right / pairs}


def percent(value: float) -> str:
    return f"{100 * value:5.1f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="the collector's folder"
    )
    parser.add_argument(
        "--models",
        type=Path,
        required=True,
        help="folder with the downloaded checkpoints",
    )
    parser.add_argument("--split", choices=("validation", "test"), default="validation")
    parser.add_argument(
        "--open-test", action="store_true", help="required with --split test"
    )
    parser.add_argument(
        "--only", nargs="*", default=None, help="measure only these candidates, by name"
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.split == "test" and not args.open_test:
        raise SystemExit(
            "the test split is opened only for the final evaluation"
            " (RFC-042 section 4.6): pass --open-test"
        )

    results_dir = args.out / "measure"
    results_dir.mkdir(exist_ok=True)
    if args.split == "test":
        marker = results_dir / "test-opened.txt"
        if not marker.exists():
            marker.write_text(
                f"test split first opened {datetime.now(UTC).isoformat()}\n",
                encoding="utf-8",
            )

    rows = [
        json.loads(line)
        for line in (HERE / "eval_set.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    gallery_paths = [args.out / row["path"] for row in rows]
    chosen = [i for i, row in enumerate(rows) if row["split"] == args.split]
    southern = [i for i in chosen if "pt_south" in rows[i]]
    languages = {
        "en": ([rows[i]["en"] for i in chosen], chosen),
        "pt": ([rows[i]["pt"] for i in chosen], chosen),
        "pt (sul)": ([rows[i]["pt"] for i in southern], southern),
        "sul": ([rows[i]["pt_south"] for i in southern], southern),
    }
    print(f"{args.split}: {len(chosen)} queries against {len(rows)} photos;", end="")
    print(f" {len(southern)} with a southern variant")

    device = resolve_device()
    runtime = default_runtime()
    translator = CachingTranslator(
        MarianQueryTranslator(runtime.loader(TRANSLATOR), device=device)
    )
    builders: list[tuple[str, Callable[[], Candidate]]] = [
        (CURRENT, partial(current_candidate, translator, device))
    ]
    for name, relative, architecture in OPEN_CLIP_CANDIDATES:
        if args.only is not None and name not in args.only:
            continue
        build = partial(
            open_clip_candidate,
            name,
            args.models / relative,
            architecture,
            translator,
            device,
        )
        builders.append((name, build))

    record: dict[str, Any] = {
        "split": args.split,
        "when": datetime.now(UTC).isoformat(),
        "device": str(device),
        "queries": len(chosen),
        "gallery": len(rows),
        "candidates": {},
    }
    ranks_by: dict[tuple[str, str, str], np.ndarray] = {}
    for _, make in builders:
        started = time.perf_counter()
        candidate = make()
        gallery = candidate.encode_images(gallery_paths)
        image_seconds = time.perf_counter() - started
        entry: dict[str, Any] = {
            "notes": candidate.notes,
            "image_seconds": image_seconds,
            "templates": {},
        }
        for template in candidate.templates:
            per_language: dict[str, Any] = {}
            for language, (queries, indices) in languages.items():
                rank = ranks(
                    candidate.encode_texts(queries, template), gallery, indices
                )
                ranks_by[(candidate.name, template, language)] = rank
                per_language[language] = {**summary(rank), "ranks": rank.tolist()}
            entry["templates"][template] = {
                "eval": per_language,
                "demo": demo_benchmark(candidate, template),
            }
        record["candidates"][candidate.name] = entry
        print(f"  {candidate.name}: done in {time.perf_counter() - started:.0f} s")
        del candidate
    record["translations"] = translator.cache
    record["translation_seconds"] = translator.seconds

    size = f"{len(chosen)} queries x {len(rows)} photos"
    print(f"\nRFC-042 5.1.2, {args.split} split ({size}), in %")
    heading = f"{'candidate':30} {'template':16} {'lang':9}"
    heading += f" {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR':>6}"
    print(heading + "   R@1 vs current [95%]      R@5 vs current [95%]")
    for name, entry in record["candidates"].items():
        for template, data in entry["templates"].items():
            for language, stats in data["eval"].items():
                line = f"{name:30} {template:16} {language:9} "
                line += " ".join(
                    percent(stats[key]) for key in ("R@1", "R@5", "R@10", "MRR")
                )
                if name != CURRENT:
                    current = ranks_by[(CURRENT, TEXT_PROMPT_TEMPLATE, language)]
                    mine = ranks_by[(name, template, language)]
                    for k in (1, 5):
                        diff, low, high = paired_interval(mine, current, k)
                        line += f"   {100 * diff:+5.1f}"
                        line += f" [{100 * low:+5.1f}, {100 * high:+5.1f}]"
                print(line)

    print("\nby scene, Portuguese through the translator, R@5 in % (n)")
    for name, entry in record["candidates"].items():
        for template, data in entry["templates"].items():
            rank = np.asarray(data["eval"]["pt"]["ranks"])
            parts = []
            for label, test in (
                ("água", lambda r: r["scene"] == "água"),
                ("construído", lambda r: r["scene"] == "construído"),
                ("campo", lambda r: r["scene"] == "campo"),
                ("fala de água", lambda r: r["water"]),
                ("não fala", lambda r: not r["water"]),
            ):
                mask = np.asarray([test(rows[i]) for i in chosen])
                share = percent(float((rank[mask] <= 5).mean())).strip()
                parts.append(f"{label} {share} ({int(mask.sum())})")
            print(f"  {name:30} {template:16} " + ", ".join(parts))

    print("\nRFC-023 benchmark (RFC-022 corpus, 25 queries x 45 photos, English), in %")
    for name, entry in record["candidates"].items():
        for template, data in entry["templates"].items():
            demo = data["demo"]
            print(
                f"  {name:30} {template:16} strict {percent(demo['strict-EN'])}", end=""
            )
            print(f"   pairwise {percent(demo['pairwise-EN'])}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = results_dir / f"baseline-{args.split}-{stamp}.json"
    target.write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        f"\ntranslation: {len(translator.cache)} queries in {translator.seconds:.0f} s"
    )
    print(f"results in {target}")


if __name__ == "__main__":
    main()
