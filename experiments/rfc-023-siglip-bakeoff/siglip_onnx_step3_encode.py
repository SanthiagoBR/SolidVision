"""RFC-023 ONNX check, step 3/3: encode with ONE model, save embeddings, exit.

Own process per (precision, tower) combination -- called four times by the
orchestrator (fp32/vision, fp32/text, int8/vision, int8/text), each a
completely fresh process holding exactly one ONNX Runtime session. See
siglip_onnx_step1_export.py's docstring and ../README.md for why: the
first attempt at this whole check crashed the machine, very likely from
holding four sessions plus the original PyTorch model in memory at once.

Session options also deliberately trade a little speed for lower, more
predictable memory: the CPU memory arena and memory-pattern optimizer are
both disabled. Worth revisiting once this class of hardware's headroom is
better understood -- right now, not crashing again matters more than the
last bit of throughput.

Saves embeddings as .npz (path/text -> vector, order-preserving via a
parallel keys.json) rather than returning them in-process, since the
whole point is that this process's memory disappears when it exits.

Usage:
    python siglip_onnx_step3_encode.py <fp32|int8> <vision|text>
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

MODEL_ID = "google/siglip2-base-patch16-384"
IMAGE_BATCH_SIZE = 32  # kept default per siglip_batching_check.py


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "dataset_tools" / "manifest.py").is_file():
            return candidate
    raise SystemExit(f"Could not locate the SolidVision repo root by walking up from {start}.")


REPO_ROOT = find_repo_root(Path(__file__).resolve())
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from PIL import Image  # noqa: E402
from transformers import AutoProcessor  # noqa: E402

from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest  # noqa: E402

EXPORT_DIR = Path(__file__).with_name("onnx_export")
RESULTS_DIR = Path(__file__).with_name("onnx_embeddings")
QUERIES_PATH = DEMO_MANIFEST_PATH.parent / "queries.json"


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def make_session(model_path: Path) -> ort.InferenceSession:
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    return ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])


def encode_images(session: ort.InferenceSession, processor, paths: list[Path]) -> tuple[list[str], np.ndarray, float]:
    keys: list[str] = []
    vectors: list[np.ndarray] = []
    start = time.perf_counter()
    for batch_paths in chunk(paths, IMAGE_BATCH_SIZE):
        images = [Image.open(p).convert("RGB") for p in batch_paths]
        pixel_values = processor(images=images, return_tensors="pt")["pixel_values"].numpy()
        out = session.run(["image_embeds"], {"pixel_values": pixel_values})[0]
        keys.extend(str(p) for p in batch_paths)
        vectors.extend(out)
    elapsed = time.perf_counter() - start
    return keys, np.stack(vectors), elapsed


def encode_texts(session: ort.InferenceSession, processor, texts: list[str]) -> tuple[list[str], np.ndarray, float]:
    keys: list[str] = []
    vectors: list[np.ndarray] = []
    start = time.perf_counter()
    for text in texts:
        input_ids = processor(text=[text], padding="max_length", return_tensors="pt")["input_ids"].numpy()
        out = session.run(["text_embeds"], {"input_ids": input_ids})[0]
        keys.append(text)
        vectors.append(out[0])
    elapsed = time.perf_counter() - start
    return keys, np.stack(vectors), elapsed


# Must match queries.json exactly.
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


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("fp32", "int8") or sys.argv[2] not in ("vision", "text"):
        raise SystemExit(f"Usage: python {sys.argv[0]} <fp32|int8> <vision|text>")
    precision, tower = sys.argv[1], sys.argv[2]

    RESULTS_DIR.mkdir(exist_ok=True)
    model_path = EXPORT_DIR / f"{tower}_{precision}.onnx"
    if not model_path.exists():
        raise SystemExit(f"{model_path} not found -- run step 1 (export) and step 2 (quantize) first.")

    processor = AutoProcessor.from_pretrained(MODEL_ID)
    session = make_session(model_path)

    if tower == "vision":
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
        paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]
        print(f"Encoding {len(paths)} images with {model_path.name} (batch={IMAGE_BATCH_SIZE})...")
        keys, vectors, elapsed = encode_images(session, processor, paths)
    else:
        queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]
        texts: list[str] = []
        for entry in queries:
            texts.append(entry["query"])
            translation = PORTUGUESE_TRANSLATIONS.get(entry["query"])
            if translation is None:
                raise SystemExit(f"Missing PT translation for: {entry['query']!r}")
            texts.append(translation)
        print(f"Encoding {len(texts)} texts with {model_path.name} (batch=1)...")
        keys, vectors, elapsed = encode_texts(session, processor, texts)

    print(f"  done in {elapsed:.2f}s ({elapsed / len(keys):.3f}s/item)")

    out_path = RESULTS_DIR / f"{tower}_{precision}.npz"
    np.savez(out_path, vectors=vectors, elapsed_seconds=elapsed)
    keys_path = RESULTS_DIR / f"{tower}_{precision}_keys.json"
    keys_path.write_text(json.dumps(keys), encoding="utf-8")
    print(f"  wrote {out_path} and {keys_path}")


if __name__ == "__main__":
    main()
