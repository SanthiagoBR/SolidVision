"""RFC-023 optimization check: does batched encoding help CLIP indexing throughput?

Same question siglip_batching_check.py answered for SigLIP2, now run
against the model the bake-off actually chose (see ../README.md, "Decisão
final"): laion/CLIP-ViT-B-32-laion2B-s34B-b79K, 512-dim, fp32, CPU. The
SigLIP number does not transfer -- different architecture, different
per-image cost -- so this needs its own measurement before batching can be
assumed safe/worthwhile for the real adapter's bulk-indexing path.

Two things measured, not just one:
  1. Speed -- amortized seconds/image at each batch size, against a
     freshly measured batch-of-1 baseline.
  2. Correctness -- cosine similarity between each image's batch-of-1
     embedding and its embedding when encoded as part of a larger batch.
     Batching is a pure computational reorganization, not a model change,
     so this should be ~1.0; if it is not, something about padding or
     batch-dependent normalization is silently corrupting embeddings, and
     that needs to be caught before trusting any speed number here.

Text side uses CLIP's own padding convention (padding=True, truncate at
77 tokens) rather than SigLIP's padding="max_length" -- see
clip_jina_bakeoff.py's note on why the two families pad differently.

Run:
    python clip_batching_check.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

MODEL_ID = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
IMAGE_BATCH_SIZES = [1, 2, 4, 8, 16, 32, 64, 128]
TEXT_BATCH_SIZES = [1, 5, 25]


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

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def encode_image_batches(
    model, processor, paths: list[Path], batch_size: int
) -> tuple[dict[Path, torch.Tensor], float]:
    embeddings: dict[Path, torch.Tensor] = {}
    images_by_path = {p: Image.open(p).convert("RGB") for p in paths}

    start = time.perf_counter()
    for batch_paths in chunk(paths, batch_size):
        batch_images = [images_by_path[p] for p in batch_paths]
        inputs = processor(images=batch_images, return_tensors="pt")
        with torch.no_grad():
            output = model.get_image_features(**inputs)
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        for path, vec in zip(batch_paths, normalized, strict=True):
            embeddings[path] = vec
    elapsed = time.perf_counter() - start

    return embeddings, elapsed


def encode_text_batches(
    model, processor, texts: list[str], batch_size: int
) -> tuple[dict[str, torch.Tensor], float]:
    embeddings: dict[str, torch.Tensor] = {}

    start = time.perf_counter()
    for batch_texts in chunk(texts, batch_size):
        inputs = processor(
            text=batch_texts, padding=True, truncation=True, return_tensors="pt"
        )
        with torch.no_grad():
            output = model.get_text_features(**inputs)
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        for text, vec in zip(batch_texts, normalized, strict=True):
            embeddings[text] = vec
    elapsed = time.perf_counter() - start

    return embeddings, elapsed


def cosine_drift(baseline: dict, candidate: dict, keys: list) -> dict:
    similarities = [float(torch.dot(baseline[k], candidate[k])) for k in keys]
    return {
        "mean": statistics.mean(similarities),
        "min": min(similarities),
    }


def main() -> None:
    device = torch.device("cpu")
    print(f"Model: {MODEL_ID}")
    print("Device: cpu (forced, for consistency with the other checks in this directory)")

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]

    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]
    texts = [entry["query"] for entry in queries]

    print(f"\nLoading model ({len(image_paths)} images, {len(texts)} texts)...")
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID, dtype=torch.float32)
    model.to(device)
    model.eval()

    print(f"\n{'=' * 78}\nIMAGE BATCHING\n{'=' * 78}")
    baseline_embeddings: dict[Path, torch.Tensor] | None = None
    image_results = []
    for batch_size in IMAGE_BATCH_SIZES:
        embeddings, elapsed = encode_image_batches(model, processor, image_paths, batch_size)
        per_image = elapsed / len(image_paths)
        if baseline_embeddings is None:
            baseline_embeddings = embeddings
            drift = {"mean": 1.0, "min": 1.0}
        else:
            drift = cosine_drift(baseline_embeddings, embeddings, image_paths)
        image_results.append(
            {"batch_size": batch_size, "total_seconds": elapsed, "seconds_per_image": per_image,
             "drift_vs_batch1": drift}
        )
        print(f"  batch_size={batch_size:>3}  total={elapsed:>7.2f}s  "
              f"s/image={per_image:>6.3f}  drift_vs_batch1(mean/min)="
              f"{drift['mean']:.4f}/{drift['min']:.4f}")

    print(f"\n{'=' * 78}\nTEXT BATCHING\n{'=' * 78}")
    text_baseline: dict[str, torch.Tensor] | None = None
    text_results = []
    for batch_size in TEXT_BATCH_SIZES:
        embeddings, elapsed = encode_text_batches(model, processor, texts, batch_size)
        per_text = elapsed / len(texts)
        if text_baseline is None:
            text_baseline = embeddings
            drift = {"mean": 1.0, "min": 1.0}
        else:
            drift = cosine_drift(text_baseline, embeddings, texts)
        text_results.append(
            {"batch_size": batch_size, "total_seconds": elapsed, "seconds_per_text": per_text,
             "drift_vs_batch1": drift}
        )
        print(f"  batch_size={batch_size:>3}  total={elapsed:>7.2f}s  "
              f"s/text={per_text:>6.3f}  drift_vs_batch1(mean/min)="
              f"{drift['mean']:.4f}/{drift['min']:.4f}")

    print(f"\n{'#' * 78}\nSUMMARY\n{'#' * 78}\n")
    baseline_per_image = image_results[0]["seconds_per_image"]
    best = min(image_results, key=lambda r: r["seconds_per_image"])
    print(f"{'batch size':>10} {'s/image':>10} {'speedup vs 1':>13} {'100k projection':>18}")
    print("-" * 55)
    for r in image_results:
        speedup = baseline_per_image / r["seconds_per_image"]
        hours = (r["seconds_per_image"] * 100_000) / 3600
        print(f"{r['batch_size']:>10} {r['seconds_per_image']:>10.3f} {speedup:>12.2f}x "
              f"{hours:>14.2f}h ({hours / 24:.2f}d)")

    print(f"\nBest: batch_size={best['batch_size']}, "
          f"{baseline_per_image / best['seconds_per_image']:.2f}x faster than one-at-a-time.")
    worst_drift = min(r["drift_vs_batch1"]["min"] for r in image_results)
    print(f"Worst-case embedding drift across all batch sizes vs batch-of-1: {worst_drift:.4f} "
          f"(should be ~1.0 -- batching must not change what is computed, only how)")

    results_path = Path(__file__).with_name("clip_batching_check_results.json")
    results_path.write_text(
        json.dumps(
            {
                "model_id": MODEL_ID,
                "image_count": len(image_paths),
                "text_count": len(texts),
                "image_batches": image_results,
                "text_batches": text_results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
