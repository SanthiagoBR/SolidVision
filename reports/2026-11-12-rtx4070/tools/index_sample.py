"""Indexa algumas pastas do acervo pela composição da CLI e mede cada job.

    $env:PYTHONPATH = "backend"; $env:DATABASE_SOURCE = "embedded"
    python -m app.infrastructure.database.embedded run -- `
        .venv-gpu\\Scripts\\python.exe reports\\2026-11-12-rtx4070\\tools\\index_sample.py `
        --label Seagate --out reports\\2026-11-12-rtx4070\\index-sample.json "D:/pasta 1" "D:/pasta 2"

Cada pasta é um `python -m app.cli index --root PASTA`: o mesmo
`indexing_worker.run()`, no mesmo processo, um job por pasta. O tempo é o
relógio de parede do job inteiro (scan, EXIF, leitura, CLIP, miniaturas,
banco). Os contadores vêm da linha do job em `indexing_jobs`.

Grava no banco de desenvolvimento (é uma indexação de verdade) e, nas
miniaturas, no diretório de dados. Nas fotos, nada.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "backend")]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from sqlalchemy import text

    from app.infrastructure.persistence.session import SessionLocal
    from app.infrastructure.workers import indexing_worker

    jobs = []
    for root in args.roots:
        started = time.perf_counter()
        indexing_worker.run(root.resolve(), args.label)
        seconds = time.perf_counter() - started

        session = SessionLocal()
        try:
            row = session.execute(
                text(
                    "select status, discovered_files, processed_images, skipped_images,"
                    " failed_images, started_at, finished_at"
                    " from indexing_jobs order by created_at desc limit 1"
                )
            ).one()
        finally:
            session.close()
        processed = row.processed_images
        job = {
            "root": root.name,
            "status": row.status,
            "discovered": row.discovered_files,
            "processed": processed,
            "skipped": row.skipped_images,
            "failed": row.failed_images,
            "seconds": round(seconds, 1),
            "job_seconds": (
                round((row.finished_at - row.started_at).total_seconds(), 1)
                if row.finished_at and row.started_at
                else None
            ),
            "seconds_per_image": round(seconds / processed, 4) if processed else None,
        }
        jobs.append(job)
        print(json.dumps(job, ensure_ascii=False), flush=True)

    processed = sum(job["processed"] for job in jobs)
    seconds = sum(job["seconds"] for job in jobs)
    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "jobs": jobs,
        "total": {
            "processed": processed,
            "seconds": round(seconds, 1),
            "seconds_per_image": round(seconds / processed, 4) if processed else None,
        },
    }
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
