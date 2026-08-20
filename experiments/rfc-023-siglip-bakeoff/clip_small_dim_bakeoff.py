"""RFC-023 pre-implementation bake-off: does going below 768-dim help CLIP?

Follow-up to clip_jina_bakeoff.py, which found SigLIP 2 `base` (768-dim)
beating every 768-dim CLIP/jina-clip-v2 candidate tested. This script asks
a narrower, CLIP-only question: since ViT-L/14 CLIP (768-dim) already lost
to SigLIP on both accuracy and speed, does dropping to ViT-B (512-dim) --
smaller and faster in principle -- change that picture, or just make CLIP
worse along with making it smaller?

Three candidates, all 512-dim, all plain CLIPModel (no jina-clip-v2 this
time -- this run is CLIP-only by design):
    openai/clip-vit-base-patch32          -- smallest common CLIP baseline
    openai/clip-vit-base-patch16          -- same size class, finer patches
    laion/CLIP-ViT-B-32-laion2B-s34B-b79K -- OpenCLIP retrain of patch32
                                              on LAION-2B (mirrors the
                                              OpenAI-vs-LAION split used
                                              for the 768-dim ViT-L/14
                                              comparison in
                                              clip_jina_bakeoff.py)

Reuses run_candidate_clip from clip_jina_bakeoff.py unchanged -- it
already takes a model_id and doesn't assume any particular dimension --
plus the corpus/query loading and scoring helpers both scripts share via
siglip_bakeoff.py. Results land in the same table format as the 768-dim
comparison, so the two are directly stackable.

Run:
    python clip_small_dim_bakeoff.py
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import torch  # noqa: E402

from clip_jina_bakeoff import run_candidate_clip  # noqa: E402
from siglip_bakeoff import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    PORTUGUESE_TRANSLATIONS,
    CandidateReport,
    load_manifest,
    load_queries,
    select_device,
)

CANDIDATES: list[str] = [
    "openai/clip-vit-base-patch32",
    "openai/clip-vit-base-patch16",
    "laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
]


def main() -> None:
    device = select_device()

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]

    queries = load_queries()

    reports: list[CandidateReport] = []
    for model_id in CANDIDATES:
        print(f"\n{'=' * 70}\n{model_id}\n{'=' * 70}")
        report = run_candidate_clip(model_id, device, image_paths, queries)
        reports.append(report)

    print(f"\n\n{'#' * 78}\nSUMMARY  (device={device}, {len(image_paths)} images, "
          f"{len(queries)} EN queries + {len(PORTUGUESE_TRANSLATIONS)} PT translations)"
          f"\n{'#' * 78}\n")

    header = (
        f"{'model':45} {'dim':>5} {'strict-EN':>10} {'strict-PT':>10} "
        f"{'pairwise':>9} {'s/img (mean)':>13} {'s/img (p95)':>12} {'load s':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        p95_idx = max(0, int(len(r.image_seconds) * 0.95) - 1)
        p95 = sorted(r.image_seconds)[p95_idx]
        print(
            f"{r.label:45} {r.dim:>5} "
            f"{r.strict_accuracy_for('en'):>10.1%} "
            f"{r.strict_accuracy_for('pt'):>10.1%} "
            f"{r.pairwise_accuracy:>9.1%} "
            f"{statistics.mean(r.image_seconds):>13.3f} "
            f"{p95:>12.3f} "
            f"{r.load_seconds:>8.1f}"
        )

    print("\nPer-query strict failures (EN):")
    for r in reports:
        failures = [res.query for res in r.results if res.lang == "en" and not res.strict_pass]
        print(f"  {r.label}: {failures if failures else '(none)'}")

    print(f"\nProjected single-process wall-clock time to encode 100,000 images ({device}):")
    for r in reports:
        mean_s = statistics.mean(r.image_seconds)
        total_hours = (mean_s * 100_000) / 3600
        print(f"  {r.label}: {total_hours:.2f} hours ({total_hours / 24:.2f} days)")

    results_path = Path(__file__).with_name("clip_small_dim_bakeoff_results.json")
    results_path.write_text(
        json.dumps(
            {
                "device": str(device),
                "device_name": (
                    torch.cuda.get_device_name(0) if device.type == "cuda" else "cpu"
                ),
                "image_count": len(image_paths),
                "query_count_en": len(queries),
                "query_count_pt": len(PORTUGUESE_TRANSLATIONS),
                "candidates": [
                    {
                        "model_id": r.model_id,
                        "label": r.label,
                        "dim": r.dim,
                        "load_seconds": r.load_seconds,
                        "strict_accuracy_en": r.strict_accuracy_for("en"),
                        "strict_accuracy_pt": r.strict_accuracy_for("pt"),
                        "pairwise_accuracy": r.pairwise_accuracy,
                        "image_seconds_mean": statistics.mean(r.image_seconds),
                        "image_seconds_median": statistics.median(r.image_seconds),
                        "text_seconds_mean": statistics.mean(r.text_seconds),
                        "failing_queries_en": [
                            res.query
                            for res in r.results
                            if res.lang == "en" and not res.strict_pass
                        ],
                    }
                    for r in reports
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
