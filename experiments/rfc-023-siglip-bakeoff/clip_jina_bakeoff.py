"""RFC-023 pre-implementation bake-off: non-SigLIP 768-dim alternatives.

Follow-up to siglip_bakeoff.py -- not part of the application. Answers a
narrower question than the main bake-off: now that SigLIP 2 `base`
(768-dim) is the leading checkpoint (see ../README.md), is SigLIP itself
the right *family*, or does a same-dimension CLIP-style model do better
on this corpus? Reuses the SigLIP bake-off's corpus loading, query set,
Portuguese translations, and strict/pairwise scoring predicate directly
(imported from siglip_bakeoff.py) so the numbers land in the same table
and are directly comparable to results_cpu_laptop.json.

Three candidates, all 768-dim, none of them SigLIP:
    openai/clip-vit-large-patch14           -- the canonical CLIP ViT-L/14
    laion/CLIP-ViT-L-14-laion2B-s32B-b82K    -- OpenCLIP retrain of the same
                                                 architecture on LAION-2B
    jinaai/jina-clip-v2                      -- multilingual, Matryoshka
                                                 embeddings truncated to 768

The two OpenAI-architecture CLIP checkpoints load via plain
transformers.AutoModel/AutoProcessor and (verified against this repo's
transformers==5.15.0) return a BaseModelOutputWithPooling from
get_image_features/get_text_features, same as SigLIP -- so they reuse
siglip_bakeoff.py's encode_images/encode_texts almost unchanged, just
with CLIP's own padding convention (padding=True, truncate at 77 tokens)
instead of SigLIP's fixed padding="max_length".

jina-clip-v2 is architecturally different: no get_image_features/
get_text_features pair, instead `model.encode_image(...)` /
`model.encode_text(...)` on a `trust_remote_code=True` AutoModel, native
1024-dim with Matryoshka truncation to smaller sizes via `truncate_dim=`.
Truncated to 768 here to match everything else in this comparison, not
because 768 is jina-clip-v2's natural size -- that's a real difference
from the other two candidates, not swept under the rug.

Neither CLIP checkpoint's text tower is multilingual (both are trained on
English web text) -- expect the PT numbers to suffer relative to SigLIP 2,
which was trained multilingually by design. jina-clip-v2 is multilingual,
so it's the one candidate here actually positioned to compete with SigLIP
on the PT queries specifically. That contrast is most of the point of
running this comparison rather than assuming "same dimension" means
"comparable capability."

Setup (adds to what siglip_bakeoff.py already needs -- see requirements.txt):
    pip install einops timm
    # jina-clip-v2 runs via trust_remote_code=True: it executes Python
    # code shipped in the jinaai/jina-clip-v2 model repo on the Hugging
    # Face Hub, not just weights. Only enabled for this specific,
    # well-known model ID -- not a blanket trust_remote_code policy.

Run:
    python clip_jina_bakeoff.py
"""

from __future__ import annotations

import gc
import json
import statistics
import time
from pathlib import Path

import torch  # noqa: E402
from PIL import Image  # noqa: E402
from transformers import AutoModel, AutoProcessor  # noqa: E402

import transformers.models.clip.modeling_clip as _clip_modeling  # noqa: E402

# jina-clip-v2's trust_remote_code implementation imports `clip_loss` from
# transformers.models.clip.modeling_clip -- present in the transformers
# version its model card was written against, removed in the 5.x rewrite
# this repo's SigLIP/CLIP candidates otherwise depend on (see
# siglip_bakeoff.py's own note on transformers 5.x's BaseModelOutputWithPooling
# change). Everything else jina-clip-v2 needs from that module (CLIPOutput,
# contrastive_loss, ...) is still present in 5.15 -- only `clip_loss` itself
# was dropped. Shimmed here rather than pinning an older transformers,
# since downgrading would break the CLIP candidates' get_image_features
# return type. Only used by jina-clip-v2's training-time loss computation,
# never called by the encode_image/encode_text paths this script uses --
# reimplemented for import-time correctness, not because its exact values
# matter here.
if not hasattr(_clip_modeling, "clip_loss"):
    def _clip_loss(similarity):
        caption_loss = _clip_modeling.contrastive_loss(similarity)
        image_loss = _clip_modeling.contrastive_loss(similarity.t())
        return (caption_loss + image_loss) / 2.0

    _clip_modeling.clip_loss = _clip_loss

from siglip_bakeoff import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    PORTUGUESE_TRANSLATIONS,
    CandidateReport,
    QueryResult,
    chunk,
    load_manifest,
    load_queries,
    select_device,
)

# (model_id, family) -- family picks the encode adapter below. All three
# are 768-dim; jina-clip-v2 gets there via truncate_dim, the other two
# natively.
CANDIDATES: list[tuple[str, str]] = [
    ("openai/clip-vit-large-patch14", "clip"),
    ("laion/CLIP-ViT-L-14-laion2B-s32B-b82K", "clip"),
    ("jinaai/jina-clip-v2", "jina"),
]

JINA_TRUNCATE_DIM = 768


# ---------------------------------------------------------------------------
# CLIP family: openai/clip-vit-large-patch14, laion/CLIP-ViT-L-14-laion2B-s32B-b82K
#
# Same get_image_features/get_text_features -> BaseModelOutputWithPooling
# .pooler_output shape as siglip_bakeoff.py's encode_images/encode_texts,
# verified against transformers==5.15.0's CLIPModel source directly rather
# than assumed. Only real difference: CLIP's text tower wants dynamic
# padding truncated at its 77-token context length, not SigLIP's fixed
# padding="max_length".
# ---------------------------------------------------------------------------


def encode_images_clip(
    model, processor, device: torch.device, paths: list[Path], batch_size: int = 1
) -> tuple[dict[Path, torch.Tensor], list[float]]:
    embeddings: dict[Path, torch.Tensor] = {}
    timings: list[float] = []
    images_by_path = {p: Image.open(p).convert("RGB") for p in paths}
    for batch_paths in chunk(paths, batch_size):
        batch_images = [images_by_path[p] for p in batch_paths]
        inputs = processor(images=batch_images, return_tensors="pt").to(device)
        if device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            output = model.get_image_features(**inputs)
        if device.type == "cuda":
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        timings.extend([elapsed / len(batch_paths)] * len(batch_paths))
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        for path, vec in zip(batch_paths, normalized, strict=True):
            embeddings[path] = vec.cpu()
    return embeddings, timings


def encode_texts_clip(
    model, processor, device: torch.device, texts: list[str]
) -> tuple[dict[str, torch.Tensor], list[float]]:
    embeddings: dict[str, torch.Tensor] = {}
    timings: list[float] = []
    for text in texts:
        inputs = processor(
            text=[text], padding=True, truncation=True, return_tensors="pt"
        ).to(device)
        if device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            output = model.get_text_features(**inputs)
        if device.type == "cuda":
            torch.cuda.synchronize()
        timings.append(time.perf_counter() - start)
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        embeddings[text] = normalized.squeeze(0).cpu()
    return embeddings, timings


def run_candidate_clip(
    model_id: str, device: torch.device, image_paths: list[Path], queries: list[dict]
) -> CandidateReport:
    start = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, dtype=torch.float32)
    model.to(device)
    model.eval()
    load_seconds = time.perf_counter() - start
    print(f"  loaded in {load_seconds:.1f}s")

    image_embeddings, image_timings = encode_images_clip(model, processor, device, image_paths)
    print(
        f"  encoded {len(image_paths)} images: mean {statistics.mean(image_timings):.3f}s  "
        f"median {statistics.median(image_timings):.3f}s"
    )

    all_texts = _all_query_texts(queries)
    text_embeddings, text_timings = encode_texts_clip(model, processor, device, all_texts)
    print(
        f"  encoded {len(all_texts)} texts: mean {statistics.mean(text_timings):.3f}s  "
        f"median {statistics.median(text_timings):.3f}s"
    )

    dim = next(iter(image_embeddings.values())).shape[0]
    report = _score(model_id, model_id, 1, dim, load_seconds, image_timings, text_timings,
                     image_embeddings, text_embeddings, queries)

    del model, processor
    if device.type == "cuda":
        torch.cuda.empty_cache()
    gc.collect()
    return report


# ---------------------------------------------------------------------------
# jina family: jinaai/jina-clip-v2
#
# No get_image_features/get_text_features pair -- trust_remote_code model
# with its own encode_image/encode_text methods, native 1024-dim,
# Matryoshka-truncated to JINA_TRUNCATE_DIM (768) via truncate_dim= so this
# candidate lands at the same dimension as the other two. Called one item
# at a time (batch_size=1 internally) to match how the CLIP and SigLIP
# candidates above are timed -- this is a latency comparison, not a
# best-case-throughput one.
# ---------------------------------------------------------------------------


def encode_images_jina(
    model, device: torch.device, paths: list[Path]
) -> tuple[dict[Path, torch.Tensor], list[float]]:
    embeddings: dict[Path, torch.Tensor] = {}
    timings: list[float] = []
    for path in paths:
        image = Image.open(path).convert("RGB")
        if device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            vec = model.encode_image([image], truncate_dim=JINA_TRUNCATE_DIM, batch_size=1)
        if device.type == "cuda":
            torch.cuda.synchronize()
        timings.append(time.perf_counter() - start)
        tensor = torch.as_tensor(vec[0], dtype=torch.float32)
        embeddings[path] = tensor / tensor.norm(p=2)
    return embeddings, timings


def encode_texts_jina(
    model, device: torch.device, texts: list[str]
) -> tuple[dict[str, torch.Tensor], list[float]]:
    embeddings: dict[str, torch.Tensor] = {}
    timings: list[float] = []
    for text in texts:
        if device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            vec = model.encode_text([text], truncate_dim=JINA_TRUNCATE_DIM, batch_size=1)
        if device.type == "cuda":
            torch.cuda.synchronize()
        timings.append(time.perf_counter() - start)
        tensor = torch.as_tensor(vec[0], dtype=torch.float32)
        embeddings[text] = tensor / tensor.norm(p=2)
    return embeddings, timings


def run_candidate_jina(
    model_id: str, device: torch.device, image_paths: list[Path], queries: list[dict]
) -> CandidateReport:
    start = time.perf_counter()
    model = AutoModel.from_pretrained(model_id, trust_remote_code=True, dtype=torch.float32)
    model.to(device)
    model.eval()
    load_seconds = time.perf_counter() - start
    print(f"  loaded in {load_seconds:.1f}s")

    image_embeddings, image_timings = encode_images_jina(model, device, image_paths)
    print(
        f"  encoded {len(image_paths)} images: mean {statistics.mean(image_timings):.3f}s  "
        f"median {statistics.median(image_timings):.3f}s"
    )

    all_texts = _all_query_texts(queries)
    text_embeddings, text_timings = encode_texts_jina(model, device, all_texts)
    print(
        f"  encoded {len(all_texts)} texts: mean {statistics.mean(text_timings):.3f}s  "
        f"median {statistics.median(text_timings):.3f}s"
    )

    dim = next(iter(image_embeddings.values())).shape[0]
    report = _score(model_id, model_id, 1, dim, load_seconds, image_timings, text_timings,
                     image_embeddings, text_embeddings, queries)

    del model
    if device.type == "cuda":
        torch.cuda.empty_cache()
    gc.collect()
    return report


# ---------------------------------------------------------------------------
# Shared: query text list and strict/pairwise scoring, identical predicate
# to siglip_bakeoff.py (same test_relevant_images_rank_above_hard_negatives
# logic), just factored out here since it's identical across both families.
# ---------------------------------------------------------------------------


def _all_query_texts(queries: list[dict]) -> list[str]:
    all_texts: list[str] = []
    for entry in queries:
        all_texts.append(entry["query"])
        translation = PORTUGUESE_TRANSLATIONS.get(entry["query"])
        if translation is not None:
            all_texts.append(translation)
    return all_texts


def _score(
    model_id: str,
    label: str,
    batch_size: int,
    dim: int,
    load_seconds: float,
    image_timings: list[float],
    text_timings: list[float],
    image_embeddings: dict[Path, torch.Tensor],
    text_embeddings: dict[str, torch.Tensor],
    queries: list[dict],
) -> CandidateReport:
    report = CandidateReport(
        model_id=model_id,
        label=label,
        batch_size=batch_size,
        dim=dim,
        load_seconds=load_seconds,
        image_seconds=image_timings,
        text_seconds=text_timings,
    )

    def similarity(text_vec: torch.Tensor, relative_path: str) -> float:
        image_vec = image_embeddings[DEMO_CORPUS_ROOT / relative_path]
        return float(torch.dot(text_vec, image_vec))

    for entry in queries:
        for lang, query_text in (
            ("en", entry["query"]),
            ("pt", PORTUGUESE_TRANSLATIONS.get(entry["query"])),
        ):
            if query_text is None:
                continue
            query_vec = text_embeddings[query_text]
            relevant_scores = [similarity(query_vec, p) for p in entry["relevant"]]
            hard_scores = [similarity(query_vec, p) for p in entry["hard_negatives"]]

            strict_pass = min(relevant_scores) > max(hard_scores)
            pairwise_total = len(relevant_scores) * len(hard_scores)
            pairwise_correct = sum(
                1 for r in relevant_scores for h in hard_scores if r > h
            )

            report.results.append(
                QueryResult(
                    query=entry["query"],
                    lang=lang,
                    strict_pass=strict_pass,
                    pairwise_correct=pairwise_correct,
                    pairwise_total=pairwise_total,
                )
            )

    return report


RUNNERS = {
    "clip": run_candidate_clip,
    "jina": run_candidate_jina,
}


def main() -> None:
    device = select_device()

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]

    queries = load_queries()

    reports: list[CandidateReport] = []
    for model_id, family in CANDIDATES:
        print(f"\n{'=' * 70}\n{model_id}  (family={family})\n{'=' * 70}")
        report = RUNNERS[family](model_id, device, image_paths, queries)
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

    results_path = Path(__file__).with_name("clip_jina_bakeoff_results.json")
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
