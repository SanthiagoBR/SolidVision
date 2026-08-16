"""RFC-023 pre-implementation bake-off: which SigLIP 2 checkpoint to standardize on.

Not part of the application. Exploration tooling that informed RFC-023
(SigLIP Adapter) before any implementation code was written -- see
../README.md in this directory for the results and the decision they fed.

For each of three SigLIP 2 checkpoints (so400m/1152-dim, large/1024-dim,
base/768-dim), loads it once via transformers, encodes all 45 demo-corpus
images and 18 query texts (the 9 committed in
backend/dataset/demo/queries.json, plus a hand-written Portuguese
translation of each -- the product's README advertises Portuguese search,
so multilingual capability is scored, not assumed), and scores two ways
per query:
    strict   -- min(relevant scores) > max(hard_negative scores), the
                exact predicate the committed dormant test uses in
                backend/tests/dataset/test_queries.py::
                test_relevant_images_rank_above_hard_negatives
    pairwise -- fraction of (relevant, hard_negative) pairs correctly
                ordered; strict is all-or-nothing per query and can hide
                a checkpoint that gets 9/10 pairs right

The device is auto-detected: CUDA if torch reports a usable GPU, otherwise
CPU. Precision is kept at float32 in both cases so results stay comparable
across machines -- this is meant to be a hardware comparison, not also a
precision comparison. The repo root is discovered by walking up from this
script's own location looking for backend/dataset_tools/manifest.py, so it
runs correctly regardless of where inside a checkout it's invoked from.

Standalone and self-contained. Does not touch the DB, does not touch the
committed schema, does not modify any other file in the repo. Reuses the
project's own manifest loader and demo corpus as the source of images and
ground truth (RFC-022).

Setup (Windows, NVIDIA GPU):
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install torch                      # plain PyPI wheel auto-selects CUDA
                                            # if a recent NVIDIA driver is present
    pip install transformers>=4.49 sentencepiece accelerate pillow

Setup (no NVIDIA GPU, or AMD/Intel GPU on Windows -- torch has no Windows
GPU backend for those, this will correctly fall back to CPU):
    pip install --index-url https://download.pytorch.org/whl/cpu torch
    pip install transformers>=4.49 sentencepiece accelerate pillow

Run:
    python siglip_bakeoff.py
"""

from __future__ import annotations

import gc
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "dataset_tools" / "manifest.py").is_file():
            return candidate
    raise SystemExit(
        "Could not locate the SolidVision repo root by walking up from "
        f"{start}. Place this script anywhere inside your clone of the repo "
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
    "google/siglip2-large-patch16-384",
    "google/siglip2-base-patch16-384",
]

QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"

# Hand-written Portuguese translations of the committed English queries.
# Keyed by the exact English query string in queries.json so a mismatch
# (typo, a query renamed later) fails loudly instead of silently skipping
# the PT half of the evaluation.
PORTUGUESE_TRANSLATIONS: dict[str, str] = {
    "rural property with a small lake": "propriedade rural com um pequeno lago",
    "small natural lake on a farm, not an artificial pond": "pequeno lago natural em uma fazenda, nao um tanque artificial",
    "hillside property with a swimming pool": "propriedade em encosta com piscina",
    "swimming pool at a rural home, not a natural or artificial water body": "piscina em uma casa rural, nao um corpo d'agua natural ou artificial",
    "artificial fish farming ponds in a valley": "tanques artificiais de piscicultura em um vale",
    "aquaculture ponds seen from above, not a swimming pool or lake": "tanques de piscicultura vistos de cima, nao uma piscina ou lago",
    "industrial warehouse beside farmland": "galpao industrial ao lado de uma area agricola",
    "warehouse next to a highway, not a warehouse inside a town": "galpao ao lado de uma rodovia, nao um galpao dentro de uma cidade",
    "cleared construction lot with exposed red soil": "terreno desmatado com solo vermelho exposto",
    "recently cleared building site with terraced red dirt": "terreno recem-desmatado com terraplenagem em solo vermelho",
    "town intersection with a car dealership": "cruzamento urbano com uma concessionaria de carros",
    "car dealership canopy with cars for sale, not a parking lot on a plain street": "cobertura de concessionaria com carros a venda, nao um estacionamento em uma rua comum",
    "commercial street with shops and storefronts": "rua comercial com lojas e vitrines",
    "workshops and small businesses lining a commercial street": "oficinas e pequenos comercios ao longo de uma rua comercial",
    "low-altitude view of storefronts with pedestrians and parked cars": "vista de baixa altitude de vitrines com pedestres e carros estacionados",
    "residential street lined with houses": "rua residencial ladeada de casas",
    "quiet neighborhood street with family houses and a workshop": "rua residencial tranquila com casas de familia e uma oficina",
    "hillside town overview with dense houses": "vista geral de uma cidade na encosta com casas densas",
    "suburban town spread across rolling hills in a loose grid": "cidade suburbana espalhada por colinas suaves em uma malha irregular",
    "wide drone overview of a hillside town with a large building complex": "vista aerea ampla de uma cidade na encosta com um grande complexo de edificios",
    "a black cat resting indoors": "um gato preto descansando dentro de casa",
    "a dog looking directly at the camera": "um cachorro olhando diretamente para a camera",
    "a bicycle parked on a street": "uma bicicleta estacionada em uma rua",
    "a ceramic coffee cup and saucer": "uma xicara de cafe de ceramica com pires",
    "a person working on a laptop indoors": "uma pessoa trabalhando em um laptop dentro de casa",
}


def select_device() -> torch.device:
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        total_mem_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        print(f"CUDA available: {name} ({total_mem_gb:.1f} GB VRAM) -- using GPU")
        return torch.device("cuda")
    print("No CUDA GPU detected by torch -- using CPU")
    return torch.device("cpu")


@dataclass
class QueryResult:
    query: str
    lang: str
    strict_pass: bool
    pairwise_correct: int
    pairwise_total: int


@dataclass
class CandidateReport:
    model_id: str
    dim: int
    load_seconds: float
    image_seconds: list[float] = field(default_factory=list)
    text_seconds: list[float] = field(default_factory=list)
    results: list[QueryResult] = field(default_factory=list)

    @property
    def pairwise_accuracy(self) -> float:
        correct = sum(r.pairwise_correct for r in self.results)
        total = sum(r.pairwise_total for r in self.results)
        return correct / total if total else float("nan")

    def strict_accuracy_for(self, lang: str) -> float:
        subset = [r for r in self.results if r.lang == lang]
        return sum(r.strict_pass for r in subset) / len(subset)


def load_queries() -> list[dict]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return raw["queries"]


def encode_images(
    model, processor, device: torch.device, paths: list[Path]
) -> tuple[dict[Path, torch.Tensor], list[float]]:
    embeddings: dict[Path, torch.Tensor] = {}
    timings: list[float] = []
    for path in paths:
        image = Image.open(path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt").to(device)
        if device.type == "cuda":
            torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            output = model.get_image_features(**inputs)
        if device.type == "cuda":
            torch.cuda.synchronize()
        timings.append(time.perf_counter() - start)
        # transformers 5.x: get_image_features returns BaseModelOutputWithPooling,
        # not a bare tensor -- the pooled embedding is .pooler_output.
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        embeddings[path] = normalized.squeeze(0).cpu()
    return embeddings, timings


def encode_texts(
    model, processor, device: torch.device, texts: list[str]
) -> tuple[dict[str, torch.Tensor], list[float]]:
    embeddings: dict[str, torch.Tensor] = {}
    timings: list[float] = []
    for text in texts:
        inputs = processor(
            text=[text], padding="max_length", return_tensors="pt"
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


def run_candidate(
    model_id: str, device: torch.device, image_paths: list[Path], queries: list[dict]
) -> CandidateReport:
    print(f"\n{'=' * 70}\n{model_id}\n{'=' * 70}")

    start = time.perf_counter()
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, dtype=torch.float32)
    model.to(device)
    model.eval()
    load_seconds = time.perf_counter() - start
    print(f"  loaded in {load_seconds:.1f}s")

    image_embeddings, image_timings = encode_images(model, processor, device, image_paths)
    print(
        f"  encoded {len(image_paths)} images: "
        f"mean {statistics.mean(image_timings):.3f}s  "
        f"median {statistics.median(image_timings):.3f}s"
    )

    all_texts: list[str] = []
    for entry in queries:
        all_texts.append(entry["query"])
        translation = PORTUGUESE_TRANSLATIONS.get(entry["query"])
        if translation is not None:
            all_texts.append(translation)

    text_embeddings, text_timings = encode_texts(model, processor, device, all_texts)
    print(
        f"  encoded {len(all_texts)} texts: "
        f"mean {statistics.mean(text_timings):.3f}s  "
        f"median {statistics.median(text_timings):.3f}s"
    )

    dim = next(iter(image_embeddings.values())).shape[0]

    report = CandidateReport(
        model_id=model_id,
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

    del model, processor
    if device.type == "cuda":
        torch.cuda.empty_cache()
    gc.collect()

    return report


def main() -> None:
    device = select_device()

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]

    queries = load_queries()
    missing_translations = [
        q["query"] for q in queries if q["query"] not in PORTUGUESE_TRANSLATIONS
    ]
    if missing_translations:
        print(f"NOTE: no PT translation for {len(missing_translations)} queries: "
              f"{missing_translations}")

    reports: list[CandidateReport] = []
    for model_id in CANDIDATES:
        report = run_candidate(model_id, device, image_paths, queries)
        reports.append(report)

    print(f"\n\n{'#' * 78}\nSUMMARY  (device={device}, {len(image_paths)} images, "
          f"{len(queries)} EN queries + {len(PORTUGUESE_TRANSLATIONS)} PT translations)"
          f"\n{'#' * 78}\n")

    header = (
        f"{'model':42} {'dim':>5} {'strict-EN':>10} {'strict-PT':>10} "
        f"{'pairwise':>9} {'s/img (mean)':>13} {'s/img (p95)':>12} {'load s':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in reports:
        p95_idx = max(0, int(len(r.image_seconds) * 0.95) - 1)
        p95 = sorted(r.image_seconds)[p95_idx]
        print(
            f"{r.model_id:42} {r.dim:>5} "
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
        print(f"  {r.model_id}: {failures if failures else '(none)'}")

    # Project against the ARCHITECTURE.md section 22 100k-image target.
    # Single-process, unbatched -- matches how the original CPU run was measured.
    print(f"\nProjected single-process wall-clock time to encode 100,000 images ({device}):")
    for r in reports:
        mean_s = statistics.mean(r.image_seconds)
        total_hours = (mean_s * 100_000) / 3600
        print(f"  {r.model_id}: {total_hours:.2f} hours ({total_hours / 24:.2f} days)")

    results_path = Path(__file__).with_name("results.json")
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
