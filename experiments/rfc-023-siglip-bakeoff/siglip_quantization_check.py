"""RFC-023 optimization check: does INT8 dynamic quantization help on CPU?

Follow-up to siglip_bakeoff.py, testing the CPU speed optimization
discussed alongside the model bake-off: torch's built-in dynamic
quantization (torch.quantization.quantize_dynamic), applied to the
checkpoint the bake-off picked (google/siglip2-base-patch16-384 --
highest strict accuracy AND ~6x faster than so400m, see ../README.md).

This is deliberately CPU-only. Dynamic quantization in vanilla PyTorch
targets CPU backends (fbgemm, qnnpack, or oneDNN depending on how the
torch wheel was built -- this script probes and uses whatever the local
build actually supports) -- there is no meaningful CUDA path for it in
the stock API, unlike static/QAT quantization or GPU-specific toolchains
(TensorRT etc.), which are out of scope here. If a GPU is present this
script still forces CPU, since testing "does quantization help the GPU"
is a different question this script does not answer.

Measures two things, not just one:
  1. Speed -- seconds/image and seconds/text, fp32 vs INT8, on this CPU.
  2. Whether it's still the same model. Two independent signals:
     - embedding drift: cosine similarity between the fp32 and INT8
       embedding for the *same* image/text -- how much quantization
       perturbs each individual vector, before any query is involved.
     - retrieval accuracy: strict/pairwise scoring against queries.json,
       exactly as in the main bake-off -- whether that perturbation is
       big enough to change which images a query would actually retrieve.

Run:
    python siglip_quantization_check.py
"""

from __future__ import annotations

import copy
import json
import statistics
import sys
import time
from pathlib import Path

MODEL_ID = "google/siglip2-base-patch16-384"


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

# Must match queries.json's PT translations exactly, or the mismatch is
# silent (a KeyError only if a text is missing entirely). Kept identical
# to siglip_bakeoff.py's dict, extended for the queries added when the
# set grew from 9 to 25 (see the "Expand RFC-023 bake-off query set" commit).
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


def load_queries() -> list[dict]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return raw["queries"]


def encode_images(
    model, processor, paths: list[Path]
) -> tuple[dict[Path, torch.Tensor], list[float]]:
    embeddings: dict[Path, torch.Tensor] = {}
    timings: list[float] = []
    for path in paths:
        image = Image.open(path).convert("RGB")
        inputs = processor(images=image, return_tensors="pt")
        start = time.perf_counter()
        with torch.no_grad():
            output = model.get_image_features(**inputs)
        timings.append(time.perf_counter() - start)
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        embeddings[path] = normalized.squeeze(0)
    return embeddings, timings


def encode_texts(
    model, processor, texts: list[str]
) -> tuple[dict[str, torch.Tensor], list[float]]:
    embeddings: dict[str, torch.Tensor] = {}
    timings: list[float] = []
    for text in texts:
        inputs = processor(text=[text], padding="max_length", return_tensors="pt")
        start = time.perf_counter()
        with torch.no_grad():
            output = model.get_text_features(**inputs)
        timings.append(time.perf_counter() - start)
        features = output.pooler_output
        normalized = features / features.norm(p=2, dim=-1, keepdim=True)
        embeddings[text] = normalized.squeeze(0)
    return embeddings, timings


def score(
    queries: list[dict],
    image_embeddings: dict[Path, torch.Tensor],
    text_embeddings: dict[str, torch.Tensor],
) -> dict:
    def similarity(text_vec: torch.Tensor, relative_path: str) -> float:
        image_vec = image_embeddings[DEMO_CORPUS_ROOT / relative_path]
        return float(torch.dot(text_vec, image_vec))

    strict_en: list[bool] = []
    strict_pt: list[bool] = []
    pairwise_correct = 0
    pairwise_total = 0

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
            (strict_en if lang == "en" else strict_pt).append(strict_pass)
            pairwise_total += len(relevant_scores) * len(hard_scores)
            pairwise_correct += sum(
                1 for r in relevant_scores for h in hard_scores if r > h
            )

    return {
        "strict_accuracy_en": sum(strict_en) / len(strict_en),
        "strict_accuracy_pt": sum(strict_pt) / len(strict_pt),
        "pairwise_accuracy": pairwise_correct / pairwise_total,
    }


def embedding_drift(
    fp32: dict, int8: dict, keys: list
) -> dict:
    """Cosine similarity between the fp32 and INT8 embedding of the same input.

    Independent of any query -- measures how much quantization perturbs
    each vector by itself, before that perturbation could possibly change
    a ranking.
    """
    similarities = [float(torch.dot(fp32[k], int8[k])) for k in keys]
    return {
        "mean": statistics.mean(similarities),
        "min": min(similarities),
        "p05": sorted(similarities)[max(0, int(len(similarities) * 0.05) - 1)],
    }


def select_quantization_engine() -> str:
    """Pick a supported quantized-CPU backend.

    Available engines vary by platform and by how this exact torch wheel
    was built: fbgemm and qnnpack are the historical x86/ARM choices, but
    a CPU-only wheel may only ship the newer oneDNN backend instead (as on
    the machine this script was first run on). Prefer fbgemm for its
    wider track record, but fall back to whatever this build actually has
    rather than hardcoding one and failing on a different machine.
    """
    supported = torch.backends.quantized.supported_engines
    for preferred in ("fbgemm", "qnnpack", "onednn"):
        if preferred in supported:
            return preferred
    raise SystemExit(f"No known quantization engine in {supported!r}")


def main() -> None:
    engine = select_quantization_engine()
    torch.backends.quantized.engine = engine
    device = torch.device("cpu")
    print(f"Quantization engine: {engine} (supported on this build: "
          f"{torch.backends.quantized.supported_engines})")
    print(f"Model: {MODEL_ID}")
    print("Device: cpu (dynamic quantization has no meaningful CUDA path in "
          "stock PyTorch -- this script always forces CPU regardless of "
          "what hardware is available)")

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]
    queries = load_queries()

    all_texts: list[str] = []
    missing_translations = []
    for entry in queries:
        all_texts.append(entry["query"])
        translation = PORTUGUESE_TRANSLATIONS.get(entry["query"])
        if translation is None:
            missing_translations.append(entry["query"])
        else:
            all_texts.append(translation)
    if missing_translations:
        raise SystemExit(f"Missing PT translations for: {missing_translations}")

    print(f"\nLoading fp32 baseline ({len(image_paths)} images, {len(all_texts)} texts)...")
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    fp32_model = AutoModel.from_pretrained(MODEL_ID, dtype=torch.float32)
    fp32_model.to(device)
    fp32_model.eval()

    fp32_image_emb, fp32_image_t = encode_images(fp32_model, processor, image_paths)
    print(f"  fp32 images: mean {statistics.mean(fp32_image_t):.3f}s  "
          f"median {statistics.median(fp32_image_t):.3f}s")
    fp32_text_emb, fp32_text_t = encode_texts(fp32_model, processor, all_texts)
    print(f"  fp32 texts:  mean {statistics.mean(fp32_text_t):.3f}s  "
          f"median {statistics.median(fp32_text_t):.3f}s")
    fp32_scores = score(queries, fp32_image_emb, fp32_text_emb)
    print(f"  fp32 scores: {fp32_scores}")

    print(f"\nApplying INT8 dynamic quantization (torch.quantization.quantize_dynamic, "
          f"nn.Linear layers, {engine} backend)...")
    start = time.perf_counter()
    int8_model = torch.quantization.quantize_dynamic(
        copy.deepcopy(fp32_model), {torch.nn.Linear}, dtype=torch.qint8
    )
    int8_model.eval()
    quantize_seconds = time.perf_counter() - start
    print(f"  quantization applied in {quantize_seconds:.2f}s (one-time cost, not per-image)")

    int8_image_emb, int8_image_t = encode_images(int8_model, processor, image_paths)
    print(f"  int8 images: mean {statistics.mean(int8_image_t):.3f}s  "
          f"median {statistics.median(int8_image_t):.3f}s")
    int8_text_emb, int8_text_t = encode_texts(int8_model, processor, all_texts)
    print(f"  int8 texts:  mean {statistics.mean(int8_text_t):.3f}s  "
          f"median {statistics.median(int8_text_t):.3f}s")
    int8_scores = score(queries, int8_image_emb, int8_text_emb)
    print(f"  int8 scores: {int8_scores}")

    image_drift = embedding_drift(fp32_image_emb, int8_image_emb, image_paths)
    text_drift = embedding_drift(fp32_text_emb, int8_text_emb, all_texts)

    print(f"\n{'#' * 78}\nSUMMARY\n{'#' * 78}\n")
    image_speedup = statistics.mean(fp32_image_t) / statistics.mean(int8_image_t)
    text_speedup = statistics.mean(fp32_text_t) / statistics.mean(int8_text_t)
    print(f"{'metric':30} {'fp32':>15} {'int8':>15} {'change':>12}")
    print("-" * 74)
    print(f"{'s/image (mean)':30} {statistics.mean(fp32_image_t):>15.3f} "
          f"{statistics.mean(int8_image_t):>15.3f} {image_speedup:>11.2f}x")
    print(f"{'s/text (mean)':30} {statistics.mean(fp32_text_t):>15.3f} "
          f"{statistics.mean(int8_text_t):>15.3f} {text_speedup:>11.2f}x")
    print(f"{'strict accuracy (EN)':30} {fp32_scores['strict_accuracy_en']:>15.1%} "
          f"{int8_scores['strict_accuracy_en']:>15.1%}")
    print(f"{'strict accuracy (PT)':30} {fp32_scores['strict_accuracy_pt']:>15.1%} "
          f"{int8_scores['strict_accuracy_pt']:>15.1%}")
    print(f"{'pairwise accuracy':30} {fp32_scores['pairwise_accuracy']:>15.1%} "
          f"{int8_scores['pairwise_accuracy']:>15.1%}")
    print(f"\nEmbedding drift (cosine similarity, fp32 vs int8, same input):")
    print(f"  images: mean {image_drift['mean']:.4f}  min {image_drift['min']:.4f}  "
          f"p05 {image_drift['p05']:.4f}")
    print(f"  texts:  mean {text_drift['mean']:.4f}  min {text_drift['min']:.4f}  "
          f"p05 {text_drift['p05']:.4f}")

    total_hours_fp32 = (statistics.mean(fp32_image_t) * 100_000) / 3600
    total_hours_int8 = (statistics.mean(int8_image_t) * 100_000) / 3600
    print(f"\nProjected single-process CPU time to encode 100,000 images:")
    print(f"  fp32: {total_hours_fp32:.2f} hours ({total_hours_fp32 / 24:.2f} days)")
    print(f"  int8: {total_hours_int8:.2f} hours ({total_hours_int8 / 24:.2f} days)")

    results_path = Path(__file__).with_name("quantization_check_results.json")
    results_path.write_text(
        json.dumps(
            {
                "model_id": MODEL_ID,
                "image_count": len(image_paths),
                "text_count": len(all_texts),
                "quantize_seconds": quantize_seconds,
                "fp32": {
                    "image_seconds_mean": statistics.mean(fp32_image_t),
                    "image_seconds_median": statistics.median(fp32_image_t),
                    "text_seconds_mean": statistics.mean(fp32_text_t),
                    **fp32_scores,
                },
                "int8": {
                    "image_seconds_mean": statistics.mean(int8_image_t),
                    "image_seconds_median": statistics.median(int8_image_t),
                    "text_seconds_mean": statistics.mean(int8_text_t),
                    **int8_scores,
                },
                "image_speedup": image_speedup,
                "text_speedup": text_speedup,
                "embedding_drift": {
                    "images": image_drift,
                    "texts": text_drift,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results written to {results_path}")


if __name__ == "__main__":
    main()
