"""RFC-048 section 12: does rebuilding this machine's venv change the vectors?

    <venv python> experiments/rfc-048-conferencias/vectors_by_venv.py OUT.npz
    python experiments/rfc-048-conferencias/vectors_by_venv.py --compare A B

The first form, run from the repository root with `backend` on `PYTHONPATH`,
encodes with the app's CLIP adapter over `runtime\\models` what the RFC-035
reference file records -- the 45 demo images one by one and in batches of 8,
and each recorded prompt without the translator and the adapter -- and saves
the vectors with the versions that made them. The second compares two such
files bit for bit, and each with the reference.

`test_reference_embeddings_slow.py` compares bit for bit only on the machine
that wrote the reference, which is not this one: here it allows 1e-5. This
script is the control the RFC asks for: the old venv and the new one, on the
same processor, with the same thread count.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "backend" / "tests" / "fixtures" / "rfc035_reference_embeddings.npz"


def encode(out: Path) -> None:
    # First: it turns the offline mode on before `huggingface_hub` loads.
    import app.infrastructure.ai  # noqa: F401, I001  # isort: skip

    import platform

    import torch
    import transformers

    from app.domain.entities.image import Image
    from app.domain.value_objects.device_id import DeviceId
    from app.domain.value_objects.image_id import ImageId
    from app.domain.value_objects.image_path import ImagePath
    from app.infrastructure.ai.clip_embedding_model import (
        TEXT_PROMPT_TEMPLATE,
        ClipEmbeddingModel,
    )
    from app.infrastructure.ai.model_runtime import CLIP, default_runtime
    from app.infrastructure.ai.query_translator import QueryTranslator
    from dataset_tools.manifest import DEMO_CORPUS_ROOT

    class Verbatim(QueryTranslator):
        """The slow test's: a recorded prompt is encoded as it is."""

        def to_english(self, text: str) -> str:
            return text

    reference = np.load(REFERENCE)
    model = ClipEmbeddingModel(default_runtime().loader(CLIP), translator=Verbatim())

    def image(relative: str) -> Image:
        path = ImagePath(str(DEMO_CORPUS_ROOT / relative))
        return Image(
            id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, relative)),
            device_id=DeviceId(uuid.UUID(int=0)),
            relative_path=path,
            absolute_path=path,
            filename=Path(relative).stem,
            extension=Path(relative).suffix.lstrip(".").lower(),
        )

    images = [image(str(p)) for p in reference["image_paths"]]
    one = [model.encode_image(i).values for i in images]
    batched: list[Any] = []
    for start in range(0, len(images), 8):
        batched += [e.values for e in model.encode_images(images[start : start + 8])]
    prefix = TEXT_PROMPT_TEMPLATE.format(query="")
    prompts = [
        model.encode_text(str(p).removeprefix(prefix)).values
        for p in reference["prompts"]
    ]
    machine = {
        "python": platform.python_version(),
        "processor": platform.processor(),
        "threads": torch.get_num_threads(),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "numpy": np.__version__,
    }
    np.savez(
        out,
        image_vectors=np.asarray(one, dtype=np.float32),
        image_vectors_batched=np.asarray(batched, dtype=np.float32),
        query_vectors=np.asarray(prompts, dtype=np.float32),
        machine=json.dumps(machine),
    )
    print(json.dumps(machine))


KEYS = ("image_vectors", "image_vectors_batched", "query_vectors")


def compare(a: Path, b: Path) -> dict[str, Any]:
    first, second, reference = np.load(a), np.load(b), np.load(REFERENCE)
    result: dict[str, Any] = {
        "a": json.loads(str(first["machine"])),
        "b": json.loads(str(second["machine"])),
    }
    for key in KEYS:
        x, y, r = first[key], second[key], reference[key]
        result[key] = {
            "rows": len(x),
            "rows_bit_equal_a_b": int(
                sum(x[i].tobytes() == y[i].tobytes() for i in range(len(x)))
            ),
            "max_abs_a_b": float(np.abs(x - y).max()),
            "max_abs_a_reference": float(np.abs(x - r).max()),
            "max_abs_b_reference": float(np.abs(y - r).max()),
        }
    return result


if __name__ == "__main__":
    if sys.argv[1] == "--compare":
        print(json.dumps(compare(Path(sys.argv[2]), Path(sys.argv[3])), indent=2))
    else:
        encode(Path(sys.argv[1]))
