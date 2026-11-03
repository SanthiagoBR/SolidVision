"""Read-only: does Pillow's JPEG draft() pay off in ClipEmbeddingModel._preprocess?

Variants: current (no draft) and draft at 1/2, 1/4, 1/8. Same images, batches
of 8, one forward per batch, L2-normalized, as in encode_images. For every
batch all four variants run back to back (order rotated each batch), so drift
in CPU clock or temperature hits all variants alike.

Files are read into memory once and decoded from BytesIO. On this machine the
page cache holds only ~0.4-0.7 GB with the model loaded (measured on
2026-10-27), less than the sample, so re-opening by path would mix HDD reads
into the preparation time. Decoding from bytes gives the same pixels (the
bytes-handoff experiment showed vector delta 0).
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPOSITORY_ROOT / "backend")]

import torch  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from PIL.JpegImagePlugin import JpegImageFile  # noqa: E402

from app.infrastructure.ai.clip_embedding_model import TEXT_PROMPT_TEMPLATE  # noqa: E402
from app.infrastructure.config.settings import settings  # noqa: E402
from tests.conftest import materialized_embedding_model  # noqa: E402

BATCH = 8
MIN_SIDE = 224
VARIANTS = [("atual", 1), ("draft 1/2", 2), ("draft 1/4", 4), ("draft 1/8", 8)]


def list_supported(root: Path) -> list[Path]:
    extensions = {ext.lower() for ext in settings.supported_extensions}
    found: list[Path] = []
    for directory, _, filenames in os.walk(root, onerror=lambda error: None):
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in extensions:
                found.append(Path(directory) / filename)
    return found


def preprocess(processor, data: bytes, scale: int) -> tuple[torch.Tensor, bool, tuple, tuple]:
    """open -> (draft) -> convert -> processor. Returns tensor, drafted?, sizes."""
    with PILImage.open(io.BytesIO(data)) as opened:
        before = opened.size
        drafted = False
        if scale > 1 and isinstance(opened, JpegImageFile):  # MPO too
            w, h = opened.size
            if min(w // scale, h // scale) >= MIN_SIDE:
                opened.draft("RGB", (w // scale, h // scale))
                drafted = True
        after = opened.size
        pixels = opened.convert("RGB")
        inputs = processor(images=pixels, return_tensors="pt")
    return inputs["pixel_values"], drafted, before, after


def cpu_name() -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$p=Get-CimInstance Win32_Processor; \"$($p.Name)|$($p.NumberOfCores)|$($p.NumberOfLogicalProcessors)\""],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out
    except Exception as error:  # noqa: BLE001
        return f"? ({error})"


def topk(sim: torch.Tensor, k: int) -> torch.Tensor:
    return sim.topk(k, dim=1).indices


def overlap(a: torch.Tensor, b: torch.Tensor) -> float:
    k = a.shape[1]
    return statistics.fmean(
        len(set(x.tolist()) & set(y.tolist())) / k for x, y in zip(a, b, strict=True)
    )


def neighbour_metrics(query: torch.Tensor, index: torch.Tensor, ref_nn, ref_top5):
    sim = query @ index.T
    sim.fill_diagonal_(-float("inf"))
    nn = sim.argmax(dim=1)
    top5 = topk(sim, 5)
    return 100.0 * (nn == ref_nn).float().mean().item(), overlap(top5, ref_top5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sample", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    name, cores, logical = (cpu_name().split("|") + ["?", "?"])[:3]
    print(f"python {platform.python_version()}, {platform.platform()}")
    print(f"processador: {name}; {cores} núcleos / {logical} threads")
    print(f"torch {torch.__version__}, get_num_threads {torch.get_num_threads()}, "
          f"cpu capability {torch.backends.cpu.get_cpu_capability()}")

    t0 = time.perf_counter()
    listed = sorted(list_supported(args.root.resolve()), key=str)
    print(f"listados {len(listed)} arquivos suportados em {args.root} em {time.perf_counter() - t0:.1f} s")
    sample = random.Random(args.seed).sample(listed, args.sample) if len(listed) > args.sample else listed
    print(f"amostra: {len(sample)} (semente {args.seed})")

    # One read of every file (the "warm cache" step), kept in memory.
    t0 = time.perf_counter()
    blobs: dict[Path, bytes] = {}
    unreadable: list[tuple[str, str]] = []
    for path in sample:
        try:
            blobs[path] = path.read_bytes()
        except OSError as error:
            unreadable.append((str(path), f"read: {error}"))
    print(f"lidos {len(blobs)} arquivos, {sum(map(len, blobs.values())) / 2**20:.0f} MB, "
          f"em {time.perf_counter() - t0:.1f} s")

    # Pillow must open and fully decode it; otherwise it is left out.
    formats: collections.Counter = collections.Counter()
    megapixels: collections.Counter = collections.Counter()
    resolutions: collections.Counter = collections.Counter()
    modes: collections.Counter = collections.Counter()
    usable: list[Path] = []
    for path, data in blobs.items():
        try:
            with PILImage.open(io.BytesIO(data)) as opened:
                fmt, size, mode = opened.format, opened.size, opened.mode
                opened.convert("RGB")
        except Exception as error:  # noqa: BLE001
            unreadable.append((str(path), f"{type(error).__name__}: {error}"))
            continue
        usable.append(path)
        formats[fmt] += 1
        modes[mode] += 1
        resolutions[f"{size[0]}x{size[1]}"] += 1
        mp = size[0] * size[1] / 1e6
        bucket = "<1" if mp < 1 else "1-4" if mp < 4 else "4-9" if mp < 9 else "9-13" if mp < 13 else "13-20" if mp < 20 else ">=20"
        megapixels[bucket] += 1
    for path in list(blobs):
        if path not in set(usable):
            blobs.pop(path)
    print(f"Pillow não abriu {len(unreadable)}: {unreadable[:10]}")
    print(f"usadas: {len(usable)}")
    print(f"formatos: {dict(formats.most_common())}; modos: {dict(modes.most_common())}")
    order = ["<1", "1-4", "4-9", "9-13", "13-20", ">=20"]
    print("megapixels: " + ", ".join(f"{b}: {megapixels[b]}" for b in order if megapixels[b]))
    print(f"resoluções mais comuns: {resolutions.most_common(10)}")

    model = materialized_embedding_model()
    clip, processor = model._ensure_loaded()
    device = model._device
    print(f"device: {device}")

    # Check on one JPEG that draft really changed the size.
    for path in usable:
        with PILImage.open(io.BytesIO(blobs[path])) as opened:
            if opened.format == "MPO" and min(opened.size) // 8 >= MIN_SIDE:
                for s in (2, 4, 8):
                    _, drafted, before, after = preprocess(processor, blobs[path], s)
                    print(f"conferência draft 1/{s} em {path.name}: {before} -> {after} (drafted={drafted})")
                break

    def run_batch(paths: list[Path], scale: int):
        prep_times = []
        tensors = []
        skipped = 0
        for path in paths:
            t = time.perf_counter()
            tensor, drafted, before, _ = preprocess(processor, blobs[path], scale)
            prep_times.append(time.perf_counter() - t)
            tensors.append(tensor)
            if scale > 1 and not drafted:
                skipped += 1
        pixel_values = torch.cat(tensors, dim=0)
        t = time.perf_counter()
        with torch.no_grad():
            features = clip.get_image_features(pixel_values=pixel_values.to(device)).pooler_output
        forward = time.perf_counter() - t
        vectors = torch.nn.functional.normalize(features, p=2.0, dim=-1).cpu()
        return vectors, prep_times, forward, skipped

    # Warm-up batch, untimed, every variant.
    for _, scale in VARIANTS:
        run_batch(usable[:BATCH], scale)

    batches = [usable[i:i + BATCH] for i in range(0, len(usable), BATCH)]
    results = {label: {"vectors": [], "prep": [], "forward": 0.0, "skipped": 0} for label, _ in VARIANTS}
    t0 = time.perf_counter()
    for number, batch in enumerate(batches):
        rotation = VARIANTS[number % len(VARIANTS):] + VARIANTS[:number % len(VARIANTS)]
        for label, scale in rotation:
            vectors, prep, forward, skipped = run_batch(batch, scale)
            r = results[label]
            r["vectors"].append(vectors)
            r["prep"].extend(prep)
            r["forward"] += forward
            r["skipped"] += skipped
        if number % 10 == 0:
            print(f"  lote {number + 1}/{len(batches)}, {time.perf_counter() - t0:.0f} s", flush=True)
    for r in results.values():
        r["vectors"] = torch.cat(r["vectors"], dim=0)

    # Text queries, straight to the template (no translator).
    queries = json.loads((REPOSITORY_ROOT / "backend/dataset/demo/queries.json").read_text(encoding="utf-8"))["queries"]
    prompts = [TEXT_PROMPT_TEMPLATE.format(query=q["query"]) for q in queries]
    with torch.no_grad():
        inputs = processor(text=prompts, return_tensors="pt", padding=True)
        text = clip.get_text_features(**{k: v.to(device) for k, v in inputs.items()}).pooler_output
    text = torch.nn.functional.normalize(text, p=2.0, dim=-1).cpu()
    print(f"consultas de texto: {len(prompts)}")

    n = len(usable)
    ref = results["atual"]["vectors"]
    ref_text_sim = text @ ref.T
    ref_text_top1 = ref_text_sim.argmax(dim=1)
    ref_text_top5 = topk(ref_text_sim, 5)
    ref_sim = ref @ ref.T
    ref_sim.fill_diagonal_(-float("inf"))
    ref_nn = ref_sim.argmax(dim=1)
    ref_top5 = topk(ref_sim, 5)

    ref_total = None
    rows = []
    for label, _ in VARIANTS:
        r = results[label]
        v = r["vectors"]
        prep_median = statistics.median(r["prep"]) * 1000
        prep_mean = statistics.fmean(r["prep"]) * 1000
        total = (sum(r["prep"]) + r["forward"]) / n * 1000
        if ref_total is None:
            ref_total, ref_prep = total, prep_median
        cos = (v * ref).sum(dim=1)
        worst = int(cos.argmin())
        tsim = text @ v.T
        text_top1 = int((tsim.argmax(dim=1) == ref_text_top1).sum())
        text_top5 = overlap(topk(tsim, 5), ref_text_top5)
        img_nn, img_top5 = neighbour_metrics(v, v, ref_nn, ref_top5)
        mix_nn, mix_top5 = neighbour_metrics(v, ref, ref_nn, ref_top5)
        row = {
            "variant": label, "prep_median_ms": prep_median, "prep_mean_ms": prep_mean,
            "forward_ms_per_img": r["forward"] / n * 1000, "total_ms_per_img": total,
            "prep_speedup": ref_prep / prep_median, "total_speedup": ref_total / total,
            "no_draft_small": r["skipped"], "cos_mean": float(cos.mean()), "cos_min": float(cos.min()),
            "cos_min_file": str(usable[worst]), "text_top1_same": text_top1, "text_top5_overlap": text_top5,
            "img_nn_same_pct": img_nn, "img_top5_overlap": img_top5,
            "mixed_nn_same_pct": mix_nn, "mixed_top5_overlap": mix_top5,
        }
        rows.append(row)

    print()
    print(f"{n} imagens, {len(batches)} lotes de {BATCH}")
    print(f"{'variante':<10} {'prep med':>8} {'ganho':>6} {'modelo':>7} {'total':>7} {'ganho':>6} {'s/draft':>7} "
          f"{'cos méd':>8} {'cos mín':>8} {'txt top1':>8} {'txt top5':>8} {'img nn%':>7} {'img top5':>8} "
          f"{'mix nn%':>7} {'mix top5':>8}")
    for row in rows:
        print(f"{row['variant']:<10} {row['prep_median_ms']:>6.1f}ms {row['prep_speedup']:>5.2f}x "
              f"{row['forward_ms_per_img']:>5.1f}ms {row['total_ms_per_img']:>5.1f}ms {row['total_speedup']:>5.2f}x "
              f"{row['no_draft_small']:>7} {row['cos_mean']:>8.4f} {row['cos_min']:>8.4f} "
              f"{row['text_top1_same']:>4}/{len(prompts):<3} {row['text_top5_overlap']:>8.3f} "
              f"{row['img_nn_same_pct']:>6.1f}% {row['img_top5_overlap']:>8.3f} "
              f"{row['mixed_nn_same_pct']:>6.1f}% {row['mixed_top5_overlap']:>8.3f}")
    for row in rows[1:]:
        print(f"  {row['variant']}: cosseno mínimo em {row['cos_min_file']}")

    if args.json is not None:
        args.json.write_text(json.dumps({
            "listed": len(listed), "sample": len(sample), "usable": n, "unreadable": unreadable,
            "formats": formats, "megapixels": megapixels, "resolutions": resolutions.most_common(30),
            "cpu": name, "cores": cores, "logical": logical, "threads": torch.get_num_threads(),
            "capability": torch.backends.cpu.get_cpu_capability(), "device": str(device), "rows": rows,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
