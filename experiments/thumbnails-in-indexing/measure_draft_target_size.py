"""Draft by target size instead of a fixed 1/2: speed and vectors on E:, quality on the demo.

Experiments 5 and 7 found that how much the draft moves CLIP's vectors
depends on how close the drafted picture gets to CLIP's 224 px: 1/2 on a
12 MP photo (~1125 px left on the smaller side) gave cosine 0.9992, while
1/8 there (~280 px) and 1/2 on the small demo images (~288 px) gave ~0.98.
So instead of a fixed scale, ask Pillow for a target size:

    opened.draft("RGB", (T, T))

Pillow then picks the largest reduction (1/2, 1/4, 1/8) that keeps both
sides >= T, and none when the picture is smaller than 2T; for anything but
a JPEG (MPO included) the call does nothing. That one line is what an RFC
would write, so it is called here as is, with no condition around it.

Variants:

    atual       no draft (today)
    1/2 fixo    experiment 5's rule: 1/2 when the smaller side stays >= 224
    alvo 1000   draft("RGB", (1000, 1000)): 1/2 on 12 MP photos
    alvo 500    draft("RGB", (500, 500)): 1/4 on 12 MP photos

Part 1 repeats experiment 5 on the archive, on the same sample (seed 42, 300
files: sorted listing, then `random.Random(seed).sample`), with the same
metrics, plus the reductions each variant chose and the largest vector
difference among the files it did not reduce (must be 0). Part 2 repeats
experiment 7 on the demo corpus, where neither target should reduce
anything.

Files are read into memory once and decoded from bytes, batches of 8, all
variants back to back on each batch with the order rotating, as in
experiment 5. Read-only: nothing is written but the optional JSON.

Run it from the repository root:

    .venv/Scripts/python.exe \
        experiments/thumbnails-in-indexing/measure_draft_target_size.py --root E:/
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import platform
import random
import statistics
import time
from pathlib import Path

from measure_clip_draft import (
    BATCH,
    MIN_SIDE,
    cpu_name,
    list_supported,
    neighbour_metrics,
    overlap,
    topk,
)
from evaluate_draft_demo import DEMO_ROOT, rank

import torch
from PIL import Image as PILImage
from PIL.JpegImagePlugin import JpegImageFile

from app.infrastructure.ai.clip_embedding_model import TEXT_PROMPT_TEMPLATE
from tests.conftest import materialized_embedding_model
from tests.dataset.test_semantic_search_e2e import (
    hard_negative_contamination_at_k,
    pairwise_accuracy,
    recall_at_k,
    strict_top_1,
)

VARIANTS: list[tuple[str, tuple[str, int] | None]] = [
    ("atual", None),
    ("1/2 fixo", ("scale", 2)),
    ("alvo 1000", ("target", 1000)),
    ("alvo 500", ("target", 500)),
]


def preprocess(processor, data: bytes, variant) -> tuple[torch.Tensor, int]:  # type: ignore[no-untyped-def]
    """open -> (draft) -> convert -> processor; returns the tensor and the reduction used."""
    with PILImage.open(io.BytesIO(data)) as opened:
        before = opened.size
        if variant is not None:
            kind, value = variant
            if kind == "scale":
                if isinstance(opened, JpegImageFile):
                    width, height = before
                    if min(width // value, height // value) >= MIN_SIDE:
                        opened.draft("RGB", (width // value, height // value))
            else:
                opened.draft("RGB", (value, value))
        after = opened.size
        pixels = opened.convert("RGB")
        inputs = processor(images=pixels, return_tensors="pt")
    return inputs["pixel_values"], round(before[0] / after[0])


def run_batch(clip, processor, device, blobs: list[bytes], variant):  # type: ignore[no-untyped-def]
    """One batch through one variant: vectors, per-image prep seconds, forward seconds, reductions."""
    prep, tensors, reductions = [], [], []
    for data in blobs:
        started = time.perf_counter()
        tensor, reduction = preprocess(processor, data, variant)
        prep.append(time.perf_counter() - started)
        tensors.append(tensor)
        reductions.append(reduction)
    pixel_values = torch.cat(tensors, dim=0)
    started = time.perf_counter()
    with torch.no_grad():
        features = clip.get_image_features(pixel_values=pixel_values.to(device)).pooler_output
    forward = time.perf_counter() - started
    vectors = torch.nn.functional.normalize(features, p=2.0, dim=-1).cpu()
    return vectors, prep, forward, reductions


def encode_text(clip, processor, device, prompts: list[str]) -> torch.Tensor:  # type: ignore[no-untyped-def]
    with torch.no_grad():
        inputs = processor(text=prompts, return_tensors="pt", padding=True)
        text = clip.get_text_features(**{k: v.to(device) for k, v in inputs.items()}).pooler_output
    return torch.nn.functional.normalize(text, p=2.0, dim=-1).cpu()


def histogram(reductions: list[int]) -> str:
    counts = collections.Counter(reductions)
    return " ".join(f"1/{s}:{counts[s]}" for s in (1, 2, 4, 8) if counts[s])


def archive_part(args, clip, processor, device) -> dict:  # type: ignore[no-untyped-def]
    listed = sorted(list_supported(args.root.resolve()), key=str)
    sample = random.Random(args.seed).sample(listed, args.sample) if len(listed) > args.sample else listed
    print(f"listed {len(listed)} supported files under {args.root}; sample {len(sample)} (seed {args.seed})")

    started = time.perf_counter()
    blobs: dict[Path, bytes] = {}
    unreadable: list[str] = []
    for path in sample:
        try:
            blobs[path] = path.read_bytes()
        except OSError:
            unreadable.append(str(path))
    print(f"read {len(blobs)} files, {sum(map(len, blobs.values())) / 2**20:.0f} MB, in {time.perf_counter() - started:.1f} s")
    sizes: dict[Path, tuple[int, int]] = {}
    for path, data in list(blobs.items()):
        try:
            with PILImage.open(io.BytesIO(data)) as opened:
                sizes[path] = opened.size
                opened.convert("RGB")
        except Exception:  # noqa: BLE001
            unreadable.append(str(path))
            blobs.pop(path)
    usable = list(blobs)
    print(f"Pillow could not open {len(unreadable)}; using {len(usable)}")

    for _, variant in VARIANTS:  # warm-up, untimed
        run_batch(clip, processor, device, [blobs[p] for p in usable[:BATCH]], variant)

    batches = [usable[i : i + BATCH] for i in range(0, len(usable), BATCH)]
    results = {label: {"vectors": [], "prep": [], "forward": 0.0, "reductions": []} for label, _ in VARIANTS}
    started = time.perf_counter()
    for number, batch in enumerate(batches):
        shift = number % len(VARIANTS)
        for label, variant in VARIANTS[shift:] + VARIANTS[:shift]:
            vectors, prep, forward, reductions = run_batch(
                clip, processor, device, [blobs[p] for p in batch], variant
            )
            r = results[label]
            r["vectors"].append(vectors)
            r["prep"].extend(prep)
            r["forward"] += forward
            r["reductions"].extend(reductions)
        if number % 10 == 0:
            print(f"  batch {number + 1}/{len(batches)}, {time.perf_counter() - started:.0f} s", flush=True)
    for r in results.values():
        r["vectors"] = torch.cat(r["vectors"], dim=0)

    queries = json.loads((DEMO_ROOT / "queries.json").read_text(encoding="utf-8"))["queries"]
    text = encode_text(clip, processor, device, [TEXT_PROMPT_TEMPLATE.format(query=q["query"]) for q in queries])

    n = len(usable)
    ref = results["atual"]["vectors"]
    ref_text_sim = text @ ref.T
    ref_text_top1, ref_text_top5 = ref_text_sim.argmax(dim=1), topk(ref_text_sim, 5)
    ref_sim = ref @ ref.T
    ref_sim.fill_diagonal_(-float("inf"))
    ref_nn, ref_top5 = ref_sim.argmax(dim=1), topk(ref_sim, 5)

    rows = []
    base_prep = base_total = None
    for label, _ in VARIANTS:
        r = results[label]
        v = r["vectors"]
        prep_median = statistics.median(r["prep"]) * 1000
        prep_mean = statistics.fmean(r["prep"]) * 1000
        total = (sum(r["prep"]) + r["forward"]) / n * 1000
        if base_prep is None:
            base_prep, base_total = prep_median, total
        cos = (v * ref).sum(dim=1)
        reduced = torch.tensor([s > 1 for s in r["reductions"]])
        kept = ~reduced
        worst = int(cos.argmin())
        tsim = text @ v.T
        img_nn, img_top5 = neighbour_metrics(v, v, ref_nn, ref_top5)
        mix_nn, mix_top5 = neighbour_metrics(v, ref, ref_nn, ref_top5)
        rows.append({
            "variant": label,
            "prep_median_ms": prep_median,
            "prep_mean_ms": prep_mean,
            "forward_ms_per_img": r["forward"] / n * 1000,
            "total_ms_per_img": total,
            "prep_speedup": base_prep / prep_median,
            "total_speedup": base_total / total,
            "reductions": dict(collections.Counter(r["reductions"])),
            "cos_mean": float(cos.mean()),
            "cos_min": float(cos.min()),
            "cos_min_file": str(usable[worst]),
            "cos_min_size": list(sizes[usable[worst]]),
            "cos_mean_reduced": float(cos[reduced].mean()) if reduced.any() else 1.0,
            "max_delta_not_reduced": float((v[kept] - ref[kept]).abs().max()) if kept.any() else 0.0,
            "text_top1_same": int((tsim.argmax(dim=1) == ref_text_top1).sum()),
            "text_top5_overlap": overlap(topk(tsim, 5), ref_text_top5),
            "img_nn_same_pct": img_nn,
            "img_top5_overlap": img_top5,
            "mixed_nn_same_pct": mix_nn,
            "mixed_top5_overlap": mix_top5,
        })

    print()
    print(f"archive: {n} images, {len(batches)} batches of {BATCH}, {len(queries)} text queries")
    print(f"{'variant':<10} {'prep med':>8} {'mean':>6} {'gain':>5} {'model':>6} {'total':>6} {'gain':>5}  "
          f"{'reductions':<26} {'cos mean':>8} {'cos min':>8} {'red. mean':>9} {'dmax kept':>9} "
          f"{'txt1':>5} {'txt5':>5} {'nn%':>5} {'nn5':>5} {'mix%':>5} {'mix5':>5}")
    for row in rows:
        print(
            f"{row['variant']:<10} {row['prep_median_ms']:>6.1f}ms {row['prep_mean_ms']:>5.0f} "
            f"{row['prep_speedup']:>4.2f}x {row['forward_ms_per_img']:>4.0f}ms {row['total_ms_per_img']:>4.0f}ms "
            f"{row['total_speedup']:>4.2f}x  "
            f"{histogram([s for s, c in row['reductions'].items() for _ in range(c)]):<26} "
            f"{row['cos_mean']:>8.4f} {row['cos_min']:>8.4f} {row['cos_mean_reduced']:>9.4f} "
            f"{row['max_delta_not_reduced']:>9.1e} "
            f"{row['text_top1_same']:>2}/{len(queries)} {row['text_top5_overlap']:>5.3f} "
            f"{row['img_nn_same_pct']:>4.1f}% {row['img_top5_overlap']:>5.3f} "
            f"{row['mixed_nn_same_pct']:>4.1f}% {row['mixed_top5_overlap']:>5.3f}"
        )
    for row in rows[1:]:
        print(f"  {row['variant']}: lowest cosine on {row['cos_min_file']} {row['cos_min_size']}")
    return {"listed": len(listed), "sample": len(sample), "usable": n, "unreadable": unreadable, "rows": rows}


def demo_part(clip, processor, device) -> dict:  # type: ignore[no-untyped-def]
    queries = json.loads((DEMO_ROOT / "queries.json").read_text(encoding="utf-8"))["queries"]
    files = sorted(p for p in (DEMO_ROOT / "images").rglob("*") if p.is_file())
    paths = [p.relative_to(DEMO_ROOT).as_posix() for p in files]
    blobs = [p.read_bytes() for p in files]
    text = encode_text(clip, processor, device, [TEXT_PROMPT_TEMPLATE.format(query=q["query"]) for q in queries])
    total_pairs = sum(len(q["relevant"]) * len(q["hard_negatives"]) for q in queries)

    print()
    print(f"demo: {len(files)} images, {len(queries)} queries")
    print(f"{'variant':<10} {'reductions':<14} {'strict top-1':>14} {'Recall@5':>9} {'contam@5':>9} {'pairwise':>16} {'cos min':>8}")
    rows = []
    reference = None
    for label, variant in VARIANTS:
        vectors, reductions = [], []
        for start in range(0, len(blobs), BATCH):
            v, _, _, red = run_batch(clip, processor, device, blobs[start : start + BATCH], variant)
            vectors.append(v)
            reductions.extend(red)
        vectors = torch.cat(vectors, dim=0)
        if reference is None:
            reference = vectors
        outcomes = rank(queries, paths, text, vectors)
        row = {
            "variant": label,
            "reductions": dict(collections.Counter(reductions)),
            "strict_top_1": strict_top_1(outcomes),
            "recall_at_5": recall_at_k(outcomes, 5),
            "contamination_at_5": hard_negative_contamination_at_k(outcomes, 5),
            "pairwise": pairwise_accuracy(outcomes),
            "cos_min": float((vectors * reference).sum(dim=1).min()),
            "max_delta": float((vectors - reference).abs().max()),
        }
        rows.append(row)
        print(
            f"{label:<10} {histogram(reductions):<14} "
            f"{row['strict_top_1']:>7.1%} ({round(row['strict_top_1'] * len(queries)):>2}/{len(queries)}) "
            f"{row['recall_at_5']:>8.1%} {row['contamination_at_5']:>9.1%} "
            f"{row['pairwise']:>7.1%} ({round(row['pairwise'] * total_pairs)}/{total_pairs}) "
            f"{row['cos_min']:>8.4f}  max |delta| {row['max_delta']:.1e}"
        )
    return {"images": len(files), "queries": len(queries), "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sample", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    name, cores, logical = (cpu_name().split("|") + ["?", "?"])[:3]
    print(f"python {platform.python_version()}, {platform.platform()}")
    print(f"processor: {name}; {cores} cores / {logical} threads")
    print(f"torch {torch.__version__}, {torch.get_num_threads()} threads, "
          f"cpu capability {torch.backends.cpu.get_cpu_capability()}; Pillow {PILImage.__version__}")

    model = materialized_embedding_model()
    clip, processor = model._ensure_loaded()
    device = model._device
    print(f"device: {device}")

    archive = archive_part(args, clip, processor, device)
    demo = demo_part(clip, processor, device)

    if args.json is not None:
        args.json.write_text(
            json.dumps({"archive": archive, "demo": demo, "seed": args.seed}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
