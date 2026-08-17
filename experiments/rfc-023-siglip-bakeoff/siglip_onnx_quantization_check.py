"""RFC-023 optimization check: calibrated static INT8 quantization via ONNX Runtime.

Follow-up to siglip_quantization_check.py, testing a mechanistically
different optimization after naive dynamic quantization was rejected
there (1.44x speedup, 12-24 points of accuracy loss, ~0.76 embedding
cosine similarity -- see ../README.md). torch.quantization.quantize_dynamic
guesses activation ranges on the fly and only touches nn.Linear. Static
quantization via onnxruntime instead:
  - calibrates real activation ranges using representative data (the demo
    corpus's own images, not guessed)
  - quantizes the whole ONNX graph, not just nn.Linear
  - targets onnxruntime's CPU execution provider, which has VNNI-aware
    INT8 GEMM kernels on supporting hardware (the user's target machine,
    an Intel Core i5-1035G1 / Ice Lake, has AVX-512 + DL Boost/VNNI)

Vision tower only. Text-tower quantization was attempted and abandoned:
it drove the calibration process to 16GB+ of private committed memory on
this 16GB machine within about two minutes and had to be force-killed --
see siglip_onnx_step2_quantize.py's docstring for the diagnosis (SigLIP2's
~256k-token multilingual vocabulary makes the text tower a 1.05GB ONNX
file, and static quantization's calibration does not appear to scale
gracefully to an embedding table that size here). Text embeddings stay
fp32 throughout and are reused for scoring both the fp32-vision and
int8-vision image embeddings, so any accuracy difference measured below
is attributable only to the vision tower's quantization.

This orchestrator does NOT do any model loading or inference itself --
it only shells out to worker scripts, each its own OS process, and reads
back small result files. The first attempt at this whole check ran
everything (PyTorch model load, ONNX export, calibration, and multiple
InferenceSession objects) in one process and crashed the machine. Rather
than trust in-process cleanup (del/gc.collect()) to fully release native
(C++) allocator memory from PyTorch and ONNX Runtime, every heavy phase
runs as a separate subprocess, so the OS reclaims its memory
unconditionally on exit:

  1. siglip_onnx_step1_export.py    -- load PyTorch model, export to ONNX
  2. siglip_onnx_step2_quantize.py  -- calibrate + statically quantize (vision only)
  3. siglip_onnx_step3_encode.py    -- run ONE model, save embeddings, exit
     (called three times: fp32/vision, fp32/text, int8/vision)

This script just orchestrates those and does the (cheap, numpy-only)
scoring and drift analysis once all three embedding files exist.

Run:
    python siglip_onnx_quantization_check.py
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "onnx_embeddings"


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "dataset_tools" / "manifest.py").is_file():
            return candidate
    raise SystemExit(f"Could not locate the SolidVision repo root by walking up from {start}.")


REPO_ROOT = find_repo_root(Path(__file__).resolve())
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

import numpy as np  # noqa: E402

from dataset_tools.manifest import DEMO_MANIFEST_PATH  # noqa: E402

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"


def run_step(*args: str) -> None:
    print(f"\n{'=' * 78}\n$ {' '.join(args)}\n{'=' * 78}")
    result = subprocess.run([sys.executable, *args], cwd=SCRIPT_DIR)
    if result.returncode != 0:
        raise SystemExit(f"Step failed (exit {result.returncode}): {' '.join(args)}")


def load_embeddings(tower: str, precision: str) -> tuple[dict[str, np.ndarray], float]:
    npz = np.load(RESULTS_DIR / f"{tower}_{precision}.npz")
    keys = json.loads((RESULTS_DIR / f"{tower}_{precision}_keys.json").read_text(encoding="utf-8"))
    vectors = npz["vectors"]
    elapsed = float(npz["elapsed_seconds"])
    return dict(zip(keys, vectors, strict=True)), elapsed


def score(queries: list[dict], image_embeddings: dict, text_embeddings: dict, translations: dict) -> dict:
    def similarity(text_vec: np.ndarray, relative_path: str) -> float:
        image_vec = image_embeddings[str(DEMO_MANIFEST_PATH.parent / relative_path)]
        return float(np.dot(text_vec, image_vec))

    strict_en, strict_pt = [], []
    pairwise_correct = pairwise_total = 0

    for entry in queries:
        for lang, query_text in (("en", entry["query"]), ("pt", translations.get(entry["query"]))):
            if query_text is None:
                continue
            query_vec = text_embeddings[query_text]
            relevant_scores = [similarity(query_vec, p) for p in entry["relevant"]]
            hard_scores = [similarity(query_vec, p) for p in entry["hard_negatives"]]
            (strict_en if lang == "en" else strict_pt).append(min(relevant_scores) > max(hard_scores))
            pairwise_total += len(relevant_scores) * len(hard_scores)
            pairwise_correct += sum(1 for r in relevant_scores for h in hard_scores if r > h)

    return {
        "strict_accuracy_en": sum(strict_en) / len(strict_en),
        "strict_accuracy_pt": sum(strict_pt) / len(strict_pt),
        "pairwise_accuracy": pairwise_correct / pairwise_total,
    }


def drift(fp32: dict, other: dict) -> dict:
    sims = [
        float(np.dot(fp32[k], other[k]) / (np.linalg.norm(fp32[k]) * np.linalg.norm(other[k])))
        for k in fp32
    ]
    return {"mean": statistics.mean(sims), "min": min(sims)}


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    run_step("siglip_onnx_step1_export.py")
    run_step("siglip_onnx_step2_quantize.py")
    run_step("siglip_onnx_step3_encode.py", "fp32", "vision")
    run_step("siglip_onnx_step3_encode.py", "fp32", "text")
    run_step("siglip_onnx_step3_encode.py", "int8", "vision")

    print(f"\n{'#' * 78}\nSCORING (this process only loads small .npz files, not any model)\n{'#' * 78}")

    from siglip_onnx_step3_encode import PORTUGUESE_TRANSLATIONS

    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]

    fp32_image_emb, fp32_image_t = load_embeddings("vision", "fp32")
    text_emb, text_t = load_embeddings("text", "fp32")
    int8_image_emb, int8_image_t = load_embeddings("vision", "int8")

    # Same fp32 text embeddings reused for both scoring passes -- text is
    # never quantized here, so any accuracy delta below is attributable
    # only to the vision tower.
    fp32_scores = score(queries, fp32_image_emb, text_emb, PORTUGUESE_TRANSLATIONS)
    int8_scores = score(queries, int8_image_emb, text_emb, PORTUGUESE_TRANSLATIONS)
    image_drift = drift(fp32_image_emb, int8_image_emb)

    n_images, n_texts = len(fp32_image_emb), len(text_emb)
    fp32_s_per_image, int8_s_per_image = fp32_image_t / n_images, int8_image_t / n_images
    s_per_text = text_t / n_texts

    print(f"\n{'metric':30} {'fp32 (ONNX)':>15} {'int8 (ONNX)':>15} {'change':>12}")
    print("-" * 74)
    print(f"{'s/image (batch=32)':30} {fp32_s_per_image:>15.3f} {int8_s_per_image:>15.3f} "
          f"{fp32_s_per_image / int8_s_per_image:>11.2f}x")
    print(f"{'s/text (fp32 only, batch=1)':30} {s_per_text:>15.3f} {'--':>15}")
    print(f"{'strict accuracy (EN)':30} {fp32_scores['strict_accuracy_en']:>15.1%} "
          f"{int8_scores['strict_accuracy_en']:>15.1%}")
    print(f"{'strict accuracy (PT)':30} {fp32_scores['strict_accuracy_pt']:>15.1%} "
          f"{int8_scores['strict_accuracy_pt']:>15.1%}")
    print(f"{'pairwise accuracy':30} {fp32_scores['pairwise_accuracy']:>15.1%} "
          f"{int8_scores['pairwise_accuracy']:>15.1%}")
    print(f"\nEmbedding drift (cosine similarity, fp32-ONNX vs int8-ONNX vision, same image):")
    print(f"  images: mean {image_drift['mean']:.4f}  min {image_drift['min']:.4f}")

    hours_fp32 = (fp32_s_per_image * 100_000) / 3600
    hours_int8 = (int8_s_per_image * 100_000) / 3600
    print(f"\nProjected single-process CPU time to encode 100,000 images (batch=32):")
    print(f"  fp32 ONNX: {hours_fp32:.2f}h ({hours_fp32 / 24:.2f}d)")
    print(f"  int8 ONNX: {hours_int8:.2f}h ({hours_int8 / 24:.2f}d)")

    results_path = SCRIPT_DIR / "onnx_quantization_check_results.json"
    results_path.write_text(
        json.dumps(
            {
                "image_batch_size": 32,
                "image_count": n_images,
                "text_count": n_texts,
                "text_tower_quantized": False,
                "fp32_onnx": {"s_per_image": fp32_s_per_image, **fp32_scores},
                "int8_onnx": {"s_per_image": int8_s_per_image, **int8_scores},
                "text_s_per_query_fp32": s_per_text,
                "image_speedup": fp32_s_per_image / int8_s_per_image,
                "embedding_drift_images": image_drift,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
