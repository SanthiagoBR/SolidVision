"""Measure the product, and optionally an adapter, on the real-archive set.

    set PYTHONPATH=
    .venv\\Scripts\\python.exe experiments\\rfc-042-fine-tuning\\measure_archive.py ^
        --archive C:\\Users\\me\\rfc042\\archive [--adapter ADAPTER.npy]

It uses the same path as `train_adapter.py`: its `Encoder`, so the
product's `ClipEmbeddingModel` with RFC-047's translator, lexicon and
template. The photos are the 1600 px thumbnails `sample_archive.py` wrote,
and every vector is cached in `ARCHIVE/adapter/`. Each query of the split
is ranked against every photo of `archive_eval_set.jsonl`.

It reports recall@1, @5 and @10 and the mean reciprocal rank, in Portuguese
through the translator. The numbers are given for all queries, then apart
by origin (the person's, Claude's drafts approved as they were, drafts
edited) and by whether the photo was taken near a training photo. With
`--adapter`, the adapter is shown next to the product, with the paired
bootstrap interval of `train_adapter.py`. The English prompt the product
built for each query is saved with the results, so translation failures
can be read.

The test split needs `--open-test`, and its first opening is recorded in
`ARCHIVE/measure/test-opened.txt`.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from train_adapter import (  # noqa: E402
    Encoder,
    adapt,
    paired_interval,
    ranks,
    read_jsonl,
)

Row = dict[str, Any]
GROUPS: tuple[tuple[str, Callable[[Row], bool]], ...] = (
    ("all", lambda r: True),
    ("human", lambda r: r["origin"] == "human"),
    ("draft-approved", lambda r: r["origin"] == "draft-approved"),
    ("draft-edited", lambda r: r["origin"] == "draft-edited"),
    ("near training", lambda r: bool(r["near"])),
    ("not near", lambda r: not r["near"]),
)


def stats(rank: np.ndarray) -> dict[str, float]:
    return {
        "R@1": float((rank <= 1).mean()),
        "R@5": float((rank <= 5).mean()),
        "R@10": float((rank <= 10).mean()),
        "MRR": float((1.0 / rank).mean()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--archive", type=Path, required=True, help="sample_archive.py's folder"
    )
    parser.add_argument("--split", choices=("validation", "test"), default="validation")
    parser.add_argument(
        "--open-test", action="store_true", help="required with --split test"
    )
    parser.add_argument("--adapter", type=Path, help="a matrix from train_adapter.py")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.split == "test" and not args.open_test:
        raise SystemExit(
            "the test split is opened only for the final evaluation"
            " (RFC-042 section 4.6): pass --open-test"
        )

    results_dir = args.archive / "measure"
    results_dir.mkdir(exist_ok=True)
    if args.split == "test":
        marker = results_dir / "test-opened.txt"
        if not marker.exists():
            marker.write_text(
                f"test split first opened {datetime.now(UTC).isoformat()}\n",
                encoding="utf-8",
            )

    rows = list(read_jsonl(args.archive / "archive_eval_set.jsonl"))
    chosen = [i for i, row in enumerate(rows) if row["split"] == args.split]
    encoder = Encoder(args.archive / "adapter")
    started = time.perf_counter()
    gallery = encoder.image_vectors(
        [(row["pageid"], args.archive / row["path"]) for row in rows]
    )
    queries = [rows[i]["pt"] for i in chosen]
    texts = encoder.text_vectors(queries)
    seconds = time.perf_counter() - started
    product = ranks(texts, gallery, chosen)
    adapted = None
    if args.adapter is not None:
        matrix = torch.from_numpy(np.load(args.adapter))
        vectors = adapt(torch.from_numpy(texts), matrix).numpy()
        adapted = ranks(vectors, gallery, chosen)

    size = f"{len(chosen)} queries x {len(rows)} photos"
    print(f"real archive, {args.split} split ({size}), Portuguese, in %")
    groups: dict[str, Any] = {}
    for label, test in GROUPS:
        mask = np.asarray([test(rows[i]) for i in chosen])
        if not mask.any():
            continue
        entry: dict[str, Any] = {"n": int(mask.sum()), "product": stats(product[mask])}
        line = f"  {label:15} n={entry['n']:3}"
        for key, value in entry["product"].items():
            line += f"  {key} {100 * value:5.1f}"
        if adapted is not None:
            entry["adapter"] = stats(adapted[mask])
            for k in (1, 5):
                diff, low, high = paired_interval(adapted[mask], product[mask], k)
                entry[f"R@{k} interval"] = [diff, low, high]
                after = 100 * entry["adapter"][f"R@{k}"]
                line += f"  | adapter R@{k} {after:5.1f}"
                line += f" ({100 * diff:+.1f} [{100 * low:+.1f}, {100 * high:+.1f}])"
        groups[label] = entry
        print(line)

    prompts = {query: encoder.model.build_prompt(query) for query in queries}
    record: dict[str, Any] = {
        "split": args.split,
        "when": datetime.now(UTC).isoformat(),
        "queries": len(chosen),
        "gallery": len(rows),
        "adapter": str(args.adapter) if args.adapter else None,
        "encode_seconds": seconds,
        "groups": groups,
        "per_query": [
            {
                "pageid": rows[i]["pageid"],
                "pt": rows[i]["pt"],
                "prompt": prompts[rows[i]["pt"]],
                "rank": int(product[n]),
                **({"adapter_rank": int(adapted[n])} if adapted is not None else {}),
            }
            for n, i in enumerate(chosen)
        ],
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = results_dir / f"archive-{args.split}-{stamp}.json"
    target.write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"encoded in {seconds:.0f} s; results in {target}")


if __name__ == "__main__":
    main()
