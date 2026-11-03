"""Does CLIP's draft at 1/2 make search worse? The demo corpus has the answers.

Experiment 5 (`measure_clip_draft.py`) found that decoding JPEGs at 1/2
before CLIP's preprocessing changes the vectors a little (cosine 0.9992 on
the real archive) and keeps the top-1 of all 25 text queries. But the real
archive has no ground truth, so "changed" could not be told from "worse".
The demo corpus (`backend/dataset/demo`, 45 images) has one: for each of the
same 25 queries, the relevant images and the hard negatives. This scores
today's preprocessing and the draft with the metrics of
`tests/dataset/test_semantic_search_e2e.py`, imported from it.

Only 1/2 applies here: the demo images are ~1024x576, so at 1/4 the smaller
side would fall below 224 px and experiment 5's rule skips the draft. It is
also the harder case for the draft: after it the picture is ~288 px, close
to CLIP's 224, so less of the decoder's shortcut is averaged away than on a
12 MP photo.

Text goes straight to the template, without the translator: the queries are
English. Bytes handoff needs no test here: it decodes the same bytes, and
experiments 4 and 6 found the vectors identical.

Run it from the repository root:

    .venv/Scripts/python.exe experiments/thumbnails-in-indexing/evaluate_draft_demo.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from measure_clip_draft import BATCH, REPOSITORY_ROOT, preprocess

import torch

from app.infrastructure.ai.clip_embedding_model import TEXT_PROMPT_TEMPLATE
from tests.conftest import materialized_embedding_model
from tests.dataset.test_semantic_search_e2e import (
    QueryOutcome,
    hard_negative_contamination_at_k,
    pairwise_accuracy,
    recall_at_k,
    strict_top_1,
)

DEMO_ROOT = REPOSITORY_ROOT / "backend" / "dataset" / "demo"
VARIANTS = [("atual", 1), ("draft 1/2", 2)]


def encode_images(clip, processor, device, blobs: list[bytes], scale: int):  # type: ignore[no-untyped-def]
    """Batches of 8, one forward each, L2-normalized, as `encode_images` does."""
    vectors = []
    drafted = 0
    for start in range(0, len(blobs), BATCH):
        tensors = []
        for data in blobs[start : start + BATCH]:
            tensor, was_drafted, _, _ = preprocess(processor, data, scale)
            tensors.append(tensor)
            drafted += was_drafted
        with torch.no_grad():
            features = clip.get_image_features(
                pixel_values=torch.cat(tensors, dim=0).to(device)
            ).pooler_output
        vectors.append(torch.nn.functional.normalize(features, p=2.0, dim=-1).cpu())
    return torch.cat(vectors, dim=0), drafted


def rank(queries, paths: list[str], text: torch.Tensor, images: torch.Tensor) -> list[QueryOutcome]:  # type: ignore[no-untyped-def]
    similarities = text @ images.T
    outcomes = []
    for query, row in zip(queries, similarities, strict=True):
        order = torch.argsort(row, descending=True).tolist()
        outcomes.append(
            QueryOutcome(
                query=query["query"],
                relevant=frozenset(query["relevant"]),
                hard_negatives=frozenset(query["hard_negatives"]),
                ranked_paths=tuple(paths[i] for i in order),
                similarities=tuple(float(row[i]) for i in order),
            )
        )
    return outcomes


def pairs(outcome: QueryOutcome) -> dict[tuple[str, str], bool]:
    """Each (relevant, hard negative) pair, and whether it is in the right order."""
    return {
        (relevant, negative): outcome.rank_of(relevant) < outcome.rank_of(negative)
        for relevant in outcome.relevant
        for negative in outcome.hard_negatives
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    queries = json.loads((DEMO_ROOT / "queries.json").read_text(encoding="utf-8"))["queries"]
    files = sorted(p for p in (DEMO_ROOT / "images").rglob("*") if p.is_file())
    paths = [p.relative_to(DEMO_ROOT).as_posix() for p in files]
    named = {path for q in queries for path in (*q["relevant"], *q["hard_negatives"])}
    if missing := named - set(paths):
        raise SystemExit(f"queries.json names images that are not in the corpus: {sorted(missing)}")
    blobs = [p.read_bytes() for p in files]
    print(f"demo corpus: {len(files)} images, {len(queries)} queries")

    model = materialized_embedding_model()
    clip, processor = model._ensure_loaded()
    device = model._device
    print(f"device: {device}")

    prompts = [TEXT_PROMPT_TEMPLATE.format(query=q["query"]) for q in queries]
    with torch.no_grad():
        inputs = processor(text=prompts, return_tensors="pt", padding=True)
        text = clip.get_text_features(**{k: v.to(device) for k, v in inputs.items()}).pooler_output
    text = torch.nn.functional.normalize(text, p=2.0, dim=-1).cpu()

    results = {}
    for label, scale in VARIANTS:
        vectors, drafted = encode_images(clip, processor, device, blobs, scale)
        results[label] = {"vectors": vectors, "drafted": drafted, "outcomes": rank(queries, paths, text, vectors)}

    total_pairs = sum(len(q["relevant"]) * len(q["hard_negatives"]) for q in queries)
    print()
    print("reference, tests/dataset/test_semantic_search_e2e.py (2026-08-22, through pgvector): "
          "strict top-1 64.0%, Recall@5 84.0%, contamination@5 12.8%, pairwise 80.4% "
          "(the bake-off's numpy loop: strict 60.0%)")
    print(f"{'variant':<10} {'drafted':>8} {'strict top-1':>14} {'Recall@5':>10} {'contam@5':>9} {'pairwise':>16}")
    rows = []
    for label, _ in VARIANTS:
        outcomes = results[label]["outcomes"]
        row = {
            "variant": label,
            "drafted": results[label]["drafted"],
            "strict_top_1": strict_top_1(outcomes),
            "recall_at_5": recall_at_k(outcomes, 5),
            "contamination_at_5": hard_negative_contamination_at_k(outcomes, 5),
            "pairwise": pairwise_accuracy(outcomes),
        }
        rows.append(row)
        print(
            f"{label:<10} {row['drafted']:>4}/{len(files):<3} "
            f"{row['strict_top_1']:>7.1%} ({round(row['strict_top_1'] * len(queries)):>2}/{len(queries)}) "
            f"{row['recall_at_5']:>9.1%} {row['contamination_at_5']:>9.1%} "
            f"{row['pairwise']:>7.1%} ({round(row['pairwise'] * total_pairs)}/{total_pairs})"
        )

    today, draft = results["atual"], results["draft 1/2"]
    cosine = (today["vectors"] * draft["vectors"]).sum(dim=1)
    worst = int(cosine.argmin())
    print()
    print(f"cosine draft vs today: mean {float(cosine.mean()):.4f}, min {float(cosine.min()):.4f} ({paths[worst]})")

    print()
    print("queries whose ranking head changed (top-1, or the set of the top-5):")
    changed = 0
    flips = {"wrong->right": 0, "right->wrong": 0}
    for before, after in zip(today["outcomes"], draft["outcomes"], strict=True):
        after_pairs = pairs(after)
        for pair, right_before in pairs(before).items():
            right_after = after_pairs[pair]
            if right_before != right_after:
                flips["wrong->right" if right_after else "right->wrong"] += 1
        top1_changed = before.ranked_paths[0] != after.ranked_paths[0]
        top5_changed = set(before.ranked_paths[:5]) != set(after.ranked_paths[:5])
        if top1_changed or top5_changed:
            changed += 1
            mark = lambda path, outcome: (  # noqa: E731
                "relevant" if path in outcome.relevant
                else "hard negative" if path in outcome.hard_negatives else "other"
            )
            print(f"  '{before.query}'")
            if top1_changed:
                print(
                    f"     top-1 {before.ranked_paths[0]} ({mark(before.ranked_paths[0], before)}) -> "
                    f"{after.ranked_paths[0]} ({mark(after.ranked_paths[0], after)})"
                )
            if top5_changed:
                left = sorted(set(before.ranked_paths[:5]) - set(after.ranked_paths[:5]))
                came = sorted(set(after.ranked_paths[:5]) - set(before.ranked_paths[:5]))
                print(
                    "     top-5 lost "
                    + ", ".join(f"{p} ({mark(p, before)})" for p in left)
                    + "; gained "
                    + ", ".join(f"{p} ({mark(p, after)})" for p in came)
                )
    print(f"  {changed} of {len(queries)} queries changed; pairwise flips: {flips}")

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "images": len(files),
                    "queries": len(queries),
                    "rows": rows,
                    "cosine_mean": float(cosine.mean()),
                    "cosine_min": float(cosine.min()),
                    "cosine_min_file": paths[worst],
                    "changed_queries": changed,
                    "pairwise_flips": flips,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
