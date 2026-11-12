"""CLIP de produção em GPU e em CPU: vazão por tamanho de lote e equivalência dos vetores.

Substitui, para esta medição, o `scripts/benchmark_indexing.py`, que não roda
mais: ele monta `Image(path=...)`, e a entidade passou a ter `relative_path` e
`device_id` no RFC-027. As perguntas são as mesmas dele (RFC-024 §3.1 e §5),
mais uma que ele não fazia: **GPU contra CPU**.

    .venv-gpu\\Scripts\\python.exe reports\\2026-11-12-rtx4070\\tools\\clip_bench.py `
        --acervo D:/ --sample 512 --out reports\\2026-11-12-rtx4070\\clip-bench.json

Dois corpora:

    demo     as 45 imagens de `backend/dataset/demo` (as do Recall@5)
    acervo   `--sample` JPEGs sorteados do disco com semente fixa: fotos
             reais, com o tamanho e a decodificação das fotos reais

Os bytes de cada corpus são lidos **uma vez, antes**, e passados ao adaptador
(`encode_images(images, contents)`, o caminho do RFC-045). O tempo do disco
fica fora da vazão do modelo e é relatado à parte. O que a vazão mede é o que
o adaptador faz: decodificar com o rascunho JPEG, pré-processar, a passada
pelo modelo e a normalização.

Equivalência, por cosseno (a métrica que o HNSW ordena):

    lote N contra lote 1, no mesmo dispositivo     (RFC-024 §5)
    GPU contra CPU, no mesmo lote                  (o que muda trocar de máquina)

Nada é gravado: nem banco, nem fotos.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import random
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "backend")]

import numpy as np  # noqa: E402
import psutil  # noqa: E402
import torch  # noqa: E402

from app.domain.entities.image import Image  # noqa: E402
from app.domain.value_objects.device_id import DeviceId  # noqa: E402
from app.domain.value_objects.image_id import ImageId  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS  # noqa: E402
from app.presentation.dependencies import _build_embedding_model  # noqa: E402

DEMO_ROOT = REPO / "backend" / "dataset" / "demo" / "images"
MB = 1024 * 1024
DEVICE_ID = DeviceId(uuid.UUID(int=0))


class PeakRss:
    """O pico de RSS do processo, amostrado durante o trabalho (como o RFC-024)."""

    def __init__(self) -> None:
        self._process = psutil.Process()
        self._stop = threading.Event()
        self.peak = 0

    def __enter__(self) -> PeakRss:
        self.peak = self._process.memory_info().rss
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(0.02):
            self.peak = max(self.peak, self._process.memory_info().rss)


def _images(paths: list[Path]) -> list[Image]:
    out = []
    for path in paths:
        relative = ImagePath(path.as_posix())
        out.append(
            Image(
                id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, path.as_posix())),
                device_id=DEVICE_ID,
                relative_path=relative,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                absolute_path=ImagePath(str(path)),
            )
        )
    return out


def _demo_paths() -> list[Path]:
    return sorted(
        p for p in DEMO_ROOT.rglob("*") if p.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
    )


def _acervo_paths(root: Path, sample: int, seed: int) -> tuple[list[Path], int]:
    every = [
        Path(folder) / name
        for folder, _, names in os.walk(root)
        if "$RECYCLE.BIN" not in folder and "System Volume Information" not in folder
        for name in names
        if name.lower().endswith((".jpg", ".jpeg"))
    ]
    every.sort()
    return random.Random(seed).sample(every, min(sample, len(every))), len(every)


def _read_all(paths: list[Path]) -> tuple[list[bytes], float, int]:
    started = time.perf_counter()
    contents = [p.read_bytes() for p in paths]
    return contents, time.perf_counter() - started, sum(len(c) for c in contents)


def _model(device: str):  # type: ignore[no-untyped-def]
    model = _build_embedding_model()
    model._device = torch.device(device)  # antes da primeira carga
    return model


def _sync(device: str) -> None:
    if device == "cuda":
        torch.cuda.synchronize()


def _matrix(vectors) -> np.ndarray:  # type: ignore[no-untyped-def]
    return np.asarray([v.values for v in vectors], dtype=np.float64)


def _compare(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    cos = (a * b).sum(axis=1)
    return {
        "mean_cosine": float(cos.mean()),
        "min_cosine": float(cos.min()),
        "max_component_delta": float(np.abs(a - b).max()),
    }


def _sweep(model, device, images, contents, batch_sizes, repeats):  # type: ignore[no-untyped-def]
    rows, vectors = [], {}
    process = psutil.Process()
    for size in batch_sizes:
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
        settled = process.memory_info().rss
        timings = []
        with PeakRss() as rss:
            for _ in range(repeats):
                produced = []
                _sync(device)
                started = time.perf_counter()
                for offset in range(0, len(images), size):
                    produced.extend(
                        model.encode_images(
                            images[offset : offset + size],
                            contents[offset : offset + size],
                        )
                    )
                _sync(device)
                timings.append(time.perf_counter() - started)
        best = min(timings)
        vectors[size] = _matrix(produced)
        row = {
            "batch_size": size,
            "images": len(images),
            "seconds_best": round(best, 3),
            "seconds_median": round(statistics.median(timings), 3),
            "ms_per_image": round(1000 * best / len(images), 2),
            "images_per_second": round(len(images) / best, 1),
            "peak_rss_mb": round(rss.peak / MB, 1),
            "rss_growth_mb": round((rss.peak - settled) / MB, 1),
        }
        if device == "cuda":
            row["peak_vram_mb"] = round(torch.cuda.max_memory_allocated() / MB, 1)
        rows.append(row)
        print(f"    {device:4} batch {size:>4}: {row['ms_per_image']:8.2f} ms/img "
              f"{row['images_per_second']:8.1f} img/s  peak RSS {row['peak_rss_mb']:7.1f} MB"
              + (f"  VRAM {row['peak_vram_mb']:7.1f} MB" if device == "cuda" else ""))
    base = rows[0]["ms_per_image"]
    for row in rows:
        row["speedup_vs_batch_1"] = round(base / row["ms_per_image"], 2)
    return rows, vectors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acervo", type=Path, default=None)
    parser.add_argument("--sample", type=int, default=512)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--batch-sizes", type=int, nargs="+",
                        default=[1, 2, 4, 8, 16, 32, 64, 128])
    parser.add_argument("--cpu-batch-sizes", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    corpora: dict[str, list[Path]] = {"demo": _demo_paths()}
    report: dict = {"date": datetime.now().isoformat(timespec="seconds"),
                    "torch": torch.__version__, "corpora": {}}
    if args.acervo is not None:
        started = time.perf_counter()
        sample, total = _acervo_paths(args.acervo, args.sample, args.seed)
        corpora["acervo"] = sample
        report["acervo_listing"] = {"root": str(args.acervo), "jpegs_on_disk": total,
                                    "seconds": round(time.perf_counter() - started, 1),
                                    "sample": len(sample), "seed": args.seed}
        print(f"acervo: {total} JPEGs on disk, sample of {len(sample)}")

    models = {}
    for device in ("cuda", "cpu"):
        if device == "cuda" and not torch.cuda.is_available():
            continue
        model = _model(device)
        started = time.perf_counter()
        model.warm_up()
        models[device] = (model, round(time.perf_counter() - started, 2))
        print(f"{device}: model load {models[device][1]} s")
    report["model_load_seconds"] = {d: m[1] for d, m in models.items()}

    for name, paths in corpora.items():
        contents, read_s, total_bytes = _read_all(paths)
        images = _images(paths)
        entry: dict = {
            "images": len(paths),
            "read": {"seconds": round(read_s, 2), "mb": round(total_bytes / MB, 1),
                     "mb_per_second": round(total_bytes / MB / read_s, 1),
                     "ms_per_image": round(1000 * read_s / len(paths), 2),
                     "mean_file_mb": round(total_bytes / MB / len(paths), 2)},
            "sweep": {}, "equivalence": {},
        }
        print(f"\n{name}: {len(paths)} images, {entry['read']['mb']} MB read in "
              f"{read_s:.1f} s ({entry['read']['mb_per_second']} MB/s)")
        vectors: dict[str, dict[int, np.ndarray]] = {}
        for device, (model, _) in models.items():
            # Uma passada de aquecimento, fora da medição (CUDA, cudnn.benchmark).
            model.encode_images(images[:8], contents[:8])
            sizes = args.batch_sizes if device == "cuda" else args.cpu_batch_sizes
            if name == "acervo" and device == "cpu":
                sizes = [s for s in sizes if s <= 32]
            rows, vectors[device] = _sweep(model, device, images, contents, sizes,
                                           args.repeats)
            entry["sweep"][device] = rows
            entry["equivalence"][f"{device}: batch N vs batch 1"] = {
                str(size): _compare(vectors[device][1], matrix)
                for size, matrix in vectors[device].items()
            }
        if "cuda" in vectors and "cpu" in vectors:
            entry["equivalence"]["gpu vs cpu"] = {
                str(size): _compare(vectors["cpu"][size], vectors["cuda"][size])
                for size in vectors["cpu"] if size in vectors["cuda"]
            }
            for size, values in entry["equivalence"]["gpu vs cpu"].items():
                print(f"    gpu vs cpu, batch {size:>3}: mean cos {values['mean_cosine']:.7f}"
                      f"  min {values['min_cosine']:.7f}  max |d| {values['max_component_delta']:.2e}")
        report["corpora"][name] = entry

    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
