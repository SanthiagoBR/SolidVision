"""Prompt-template caveat check, follow-up to siglip_bakeoff.py.

SigLIP was trained on raw web captions and its own paper does not require a
CLIP-style "a photo of a {label}" template -- but that claim was never
verified against this project's own corpus, and the bake-off's strict
accuracy numbers (33-44%) were low enough to be worth ruling this out before
it becomes a load-bearing assumption in the RFC.

Only the two poles from the bake-off are re-tested (so400m and base) --
large added no new information there and re-running it would only cost
another ~11 minutes for no new signal. Image embeddings do not depend on
the text template, so each model's 45 images are encoded exactly once and
reused across both the raw and templated text variants, roughly halving
the cost of a naive rerun.

Not part of the application -- see ../README.md in this directory.

Run:
    python siglip_template_check.py
"""

from __future__ import annotations

import gc
import json
import statistics
import sys
import time
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "dataset_tools" / "manifest.py").is_file():
            return candidate
    raise SystemExit(
        "Could not locate the SolidVision repo root by walking up from "
        f"{start}. Place this script anywhere inside a clone of the repo "
        "(repo root, backend/, or a subfolder of either)."
    )


REPO_ROOT = find_repo_root(Path(__file__).resolve())
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

import torch  # noqa: E402
from PIL import Image  # noqa: E402
from transformers import AutoModel, AutoProcessor  # noqa: E402

from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest  # noqa: E402

CANDIDATES = [
    "google/siglip2-so400m-patch14-384",
    "google/siglip2-base-patch16-384",
]

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"

TEMPLATE_VARIANTS = {
    "raw": "{query}",
    "a_photo_of": "a photo of {query}",
    "aerial_photo_of": "an aerial photo of {query}",
}


def load_queries() -> list[dict]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return raw["queries"]


def encode_images(model, processor, image_paths: list[Path]) -> dict[Path, torch.Tensor]:
    embeddings: dict[Path, torch.Tensor] = {}
    for path in image_paths:
        image = Image.open(path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        with torch.no_grad():
            output = model.get_image_features(**inputs)
        features = output.pooler_output
        embeddings[path] = (features / features.norm(p=2, dim=-1, keepdim=True)).squeeze(0)
    return embeddings


def encode_text(model, processor, text: str) -> torch.Tensor:
    inputs = processor(text=[text], padding="max_length", return_tensors="pt")
    with torch.no_grad():
        output = model.get_text_features(**inputs)
    features = output.pooler_output
    return (features / features.norm(p=2, dim=-1, keepdim=True)).squeeze(0)


def score_variant(
    variant_template: str,
    queries: list[dict],
    image_embeddings: dict[Path, torch.Tensor],
    model,
    processor,
) -> tuple[float, float]:
    def similarity(text_vec: torch.Tensor, relative_path: str) -> float:
        image_vec = image_embeddings[DEMO_CORPUS_ROOT / relative_path]
        return float(torch.dot(text_vec, image_vec))

    strict_results = []
    pairwise_correct = 0
    pairwise_total = 0

    for entry in queries:
        text = variant_template.format(query=entry["query"])
        query_vec = encode_text(model, processor, text)

        relevant_scores = [similarity(query_vec, p) for p in entry["relevant"]]
        hard_scores = [similarity(query_vec, p) for p in entry["hard_negatives"]]

        strict_results.append(min(relevant_scores) > max(hard_scores))
        pairwise_total += len(relevant_scores) * len(hard_scores)
        pairwise_correct += sum(1 for r in relevant_scores for h in hard_scores if r > h)

    strict_accuracy = sum(strict_results) / len(strict_results)
    pairwise_accuracy = pairwise_correct / pairwise_total
    return strict_accuracy, pairwise_accuracy


def run_candidate(model_id: str, image_paths: list[Path], queries: list[dict]) -> dict:
    print(f"\n{'=' * 70}\n{model_id}\n{'=' * 70}")
    start = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, dtype=torch.float32)
    model.eval()
    print(f"  loaded in {time.perf_counter() - start:.1f}s")

    start = time.perf_counter()
    image_embeddings = encode_images(model, processor, image_paths)
    print(f"  encoded {len(image_paths)} images in {time.perf_counter() - start:.1f}s")

    variant_results = {}
    for variant_name, template in TEMPLATE_VARIANTS.items():
        strict_acc, pairwise_acc = score_variant(
            template, queries, image_embeddings, model, processor
        )
        variant_results[variant_name] = {
            "template": template,
            "strict_accuracy": strict_acc,
            "pairwise_accuracy": pairwise_acc,
        }
        print(
            f"  [{variant_name:16}] template={template!r:45} "
            f"strict={strict_acc:.1%}  pairwise={pairwise_acc:.1%}"
        )

    del model, processor
    gc.collect()
    return variant_results


def main() -> None:
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]
    queries = load_queries()

    all_results = {}
    for model_id in CANDIDATES:
        all_results[model_id] = run_candidate(model_id, image_paths, queries)

    print(f"\n\n{'#' * 90}\nSUMMARY: strict accuracy by template variant\n{'#' * 90}\n")
    header = f"{'model':40} " + " ".join(f"{name:>16}" for name in TEMPLATE_VARIANTS)
    print(header)
    print("-" * len(header))
    for model_id, variants in all_results.items():
        row = f"{model_id:40} " + " ".join(
            f"{variants[name]['strict_accuracy']:>16.1%}" for name in TEMPLATE_VARIANTS
        )
        print(row)

    results_path = Path(__file__).with_name("siglip_template_check_results.json")
    results_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
