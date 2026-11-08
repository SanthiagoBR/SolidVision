"""Put a level A matrix into the product as `query_adapter.safetensors`.

    .venv\\Scripts\\python.exe experiments\\rfc-042-fine-tuning\\export_adapter.py ^
        C:\\Users\\me\\rfc042\\commons\\adapter\\adapter-archive-....npy

It reads the matrix `train_adapter.py` saved and the report beside it (the
`.json` with the same name). It then writes
`backend/app/infrastructure/ai/query_adapter.safetensors`, whose header names
what the matrix was trained for:

- the lockfile's CLIP, which the app checks on load;
- the lexicon the training searches were translated with, which a test checks;
- the hyperparameters and the number of pairs.

The header is distributed with the app, so it names no path and no query:
the training photos and their searches show private places.

**The lexicon.** Reports written since the export existed carry
`lexicon_sha256`, and the lexicon of today must match it. Older reports do
not. For those, `--lexicon-as-now` records today's lexicon, and is passed
only after checking by hand that the training's text cache is keyed by
today's lexicon (`text_vectors-<sha256[:12]>.npz` in the cache folder).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import torch
from safetensors.torch import save_file

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path[:0] = [str(REPOSITORY / "backend")]

from app.infrastructure.ai.model_runtime import CLIP, read_lockfile  # noqa: E402
from app.infrastructure.ai.query_adapter import (  # noqa: E402
    ADAPTER_NAME,
    MATRIX_KEY,
    QueryAdapter,
    lexicon_digest,
)
from app.infrastructure.ai.query_translator import LEXICON_NAME  # noqa: E402

AI = REPOSITORY / "backend" / "app" / "infrastructure" / "ai"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("matrix", type=Path, help="a .npy of train_adapter.py")
    parser.add_argument(
        "--lexicon-as-now",
        action="store_true",
        help="record today's lexicon for a report that has none (see above)",
    )
    args = parser.parse_args()

    report = json.loads(args.matrix.with_suffix(".json").read_text(encoding="utf-8"))
    lexicon = lexicon_digest((AI / LEXICON_NAME).read_bytes())
    trained_with = report.get("lexicon_sha256")
    if trained_with is None and not args.lexicon_as_now:
        raise SystemExit(
            "the report does not say which lexicon the training used:"
            " check the text cache by hand, then pass --lexicon-as-now"
        )
    if trained_with is not None and trained_with != lexicon:
        raise SystemExit(
            f"trained with lexicon {trained_with[:12]}, and today's is"
            f" {lexicon[:12]}: train again"
        )

    array = np.load(args.matrix)
    matrix = torch.from_numpy(np.ascontiguousarray(array, dtype=np.float32))
    clip = read_lockfile().models[CLIP]
    hyper = report["hyper"]
    metadata = {
        "rfc": "RFC-042 level A",
        "formula": "normalize(t + t @ matrix.T)",
        "clip_repository": clip.repository,
        "clip_revision": clip.revision,
        "lexicon_sha256": lexicon,
        "trained": str(report["when"]),
        "trained_file": args.matrix.name,
        "pairs": str(report["pairs"]),
        "sources": json.dumps(report["sources"], sort_keys=True),
        "hyper": json.dumps(hyper, sort_keys=True),
        "matrix_sha256": hashlib.sha256(matrix.numpy().tobytes()).hexdigest(),
    }
    target = AI / ADAPTER_NAME
    save_file({MATRIX_KEY: matrix}, target, metadata=metadata)

    loaded = QueryAdapter.load(target)
    if not torch.equal(loaded.matrix, matrix):
        raise SystemExit(f"{target} does not read back as the matrix written")
    norm = float(matrix.norm())
    print(f"wrote {target} ({target.stat().st_size} bytes), ||A|| = {norm:.3f}")
    for key, value in metadata.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
