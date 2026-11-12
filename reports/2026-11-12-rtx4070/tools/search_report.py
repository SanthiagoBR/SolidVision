"""Latência e resultados da busca sobre o acervo indexado no banco de desenvolvimento.

Rode dentro do cluster embarcado, da raiz do repositório:

    $env:PYTHONPATH = "backend"; $env:DATABASE_SOURCE = "embedded"
    python -m app.infrastructure.database.embedded run -- `
        .venv-gpu\\Scripts\\python.exe reports\\2026-11-12-rtx4070\\tools\\search_report.py `
        --device cuda --out reports\\2026-11-12-rtx4070\\search-gpu.json

Mede, por consulta, três tempos separados -- para que o custo da tradução e o
do HNSW nunca fiquem escondidos num número só:

    encode_ms   build_prompt (idioma + tradução + template) e o encoder de texto
    prompt_ms   só o build_prompt, de dentro do encode_ms
    db_ms       só `search_similar` no PostgreSQL, com o vetor já pronto
    total_ms    `SearchImagesUseCase.execute`, o caminho da API e da CLI

Cada consulta roda uma vez a frio e `--repeats` vezes a quente; o relatório
traz a mediana a quente e o top-k com caminho, similaridade e o prompt em
inglês, para comparar o que a busca devolve entre máquinas e versões.

Também a auto-recuperação: `--self-checks` fotos do índice, sorteadas com
semente fixa, buscadas pela própria foto. A esperada é o top-1 com
similaridade ~1,0; o que não for isso aponta um vetor gravado errado.

Só lê do banco. Nada é gravado nas fotos nem no índice.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "backend")]

QUERIES: list[tuple[str, str]] = [
    # (idioma, consulta) -- o acervo é de família, eventos, viagens e drone.
    ("pt", "casamento na igreja"),
    ("pt", "noiva com vestido branco"),
    ("pt", "bolo de aniversário com velas"),
    ("pt", "crianças brincando no parque"),
    ("pt", "cachorro deitado no sofá"),
    ("pt", "pôr do sol na praia"),
    ("pt", "vista aérea de uma fazenda"),
    ("pt", "propriedade rural com lago"),
    ("pt", "casa com piscina"),
    ("pt", "rio com ponte"),
    ("pt", "cachoeira na mata"),
    ("pt", "árvore de natal com presentes"),
    ("pt", "grupo de pessoas posando para foto"),
    ("pt", "festa junina"),
    ("pt", "comida na mesa"),
    ("pt", "carro estacionado na rua"),
    ("pt", "sala de aula com alunos"),
    ("pt", "captura de tela de celular"),
    ("pt", "igreja vista de cima"),
    ("pt", "plantação de milho"),
    ("en", "a dog"),
    ("en", "aerial view of a farm"),
    ("en", "wedding cake"),
    ("en", "sunset over the sea"),
    ("en", "screenshot of a phone"),
]


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 2)


def _timed(call):  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    value = call()
    return value, time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=None, help="cuda | cpu (DEVICE)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--self-checks", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.device:
        os.environ["DEVICE"] = args.device

    import torch
    from sqlalchemy import text

    from app.application.use_cases.search_images import SearchImagesUseCase
    from app.infrastructure.config.settings import settings
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.persistence.session import SessionLocal
    from app.presentation.dependencies import get_embedding_model

    session = SessionLocal()
    repository = PostgresImageRepository(session)
    model = get_embedding_model()
    use_case = SearchImagesUseCase(
        repository=repository,
        embedding_model=model,
        default_limit=settings.top_k_results,
        min_radius_m=settings.min_radius_m,
    )

    census = {
        "images": session.execute(text("select count(*) from images")).scalar_one(),
        "with_embedding": session.execute(
            text("select count(*) from images where embedding is not null")
        ).scalar_one(),
        "devices": session.execute(text("select count(*) from devices")).scalar_one(),
        "indexes_on_images": [
            row[0]
            for row in session.execute(
                text("select indexdef from pg_indexes where tablename = 'images'")
            )
        ],
        "table_mb": session.execute(
            text("select pg_total_relation_size('images') / 1048576.0")
        ).scalar_one(),
    }
    census["table_mb"] = round(float(census["table_mb"]), 1)
    print(f"census: {census['images']} images, {census['with_embedding']} embedded")

    _, clip_load = _timed(model.warm_up)
    _, translator_load = _timed(lambda: model.build_prompt("uma casa"))
    print(f"CLIP load {clip_load:.2f}s, translator load {translator_load:.2f}s")

    def sync() -> None:
        if torch.cuda.is_available():
            torch.cuda.synchronize()

    results = []
    for language, query in QUERIES:
        runs = []
        for attempt in range(1 + args.repeats):
            prompt, prompt_s = _timed(lambda: model.build_prompt(query))
            vector, encode_s = _timed(lambda: (model.encode_text(query), sync())[0])
            hits, db_s = _timed(
                lambda: repository.search_similar(vector, limit=args.limit)
            )
            _, total_s = _timed(lambda: use_case.execute(query, limit=args.limit))
            runs.append(
                {
                    "cold": attempt == 0,
                    "prompt_ms": _ms(prompt_s),
                    "encode_ms": _ms(encode_s),
                    "db_ms": _ms(db_s),
                    "total_ms": _ms(total_s),
                }
            )
        warm = [run for run in runs if not run["cold"]]
        row = {
            "language": language,
            "query": query,
            "prompt": prompt,
            "cold": runs[0],
            "warm_median": {
                key: round(statistics.median(run[key] for run in warm), 2)
                for key in ("prompt_ms", "encode_ms", "db_ms", "total_ms")
            },
            "top": [
                {
                    "rank": rank,
                    "similarity": round(hit.similarity, 4),
                    "relative_path": str(hit.image.relative_path),
                    "device_id": str(hit.image.device_id),
                }
                for rank, hit in enumerate(hits, start=1)
            ],
        }
        results.append(row)
        print(
            f"{query!r:45} -> {prompt!r:45} total {row['warm_median']['total_ms']:7.1f} ms"
            f"  db {row['warm_median']['db_ms']:6.1f}  top1 "
            f"{row['top'][0]['similarity'] if row['top'] else '-'}"
        )

    # Auto-recuperação: a foto buscada por ela mesma.
    ids = [
        row[0]
        for row in session.execute(
            text("select id from images where embedding is not null order by id")
        )
    ]
    rng = random.Random(args.seed)
    sample = rng.sample(ids, min(args.self_checks, len(ids)))
    self_checks = []
    for image_id in sample:
        stored = session.execute(
            text("select embedding from images where id = :id"), {"id": image_id}
        ).scalar_one()
        from app.domain.value_objects.embedding_vector import EmbeddingVector

        # Sem o tipo do pgvector registrado na conexão, o SQL cru devolve o
        # texto do vetor, "[0.01,-0.02,...]".
        if isinstance(stored, str):
            values = [float(x) for x in stored.strip("[]").split(",")]
        else:
            values = [float(x) for x in stored]
        vector = EmbeddingVector(values)
        hits, db_s = _timed(lambda: repository.search_similar(vector, limit=5))
        top_ids = [str(hit.image.id.value) for hit in hits]
        self_checks.append(
            {
                "image_id": str(image_id),
                "rank_of_itself": (
                    top_ids.index(str(image_id)) + 1
                    if str(image_id) in top_ids
                    else None
                ),
                "top1_similarity": round(hits[0].similarity, 4) if hits else None,
                "db_ms": _ms(db_s),
            }
        )
    found = sum(1 for check in self_checks if check["rank_of_itself"] is not None)
    print(f"self-retrieval: {found}/{len(self_checks)} found in the top 5")

    session.close()

    warm_totals = [row["warm_median"]["total_ms"] for row in results]
    warm_db = [row["warm_median"]["db_ms"] for row in results]
    report = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "device": str(getattr(model, "_device", settings.device)),
        "census": census,
        "clip_load_seconds": round(clip_load, 2),
        "translator_load_seconds": round(translator_load, 2),
        "summary": {
            "queries": len(results),
            "warm_total_ms_median": round(statistics.median(warm_totals), 2),
            "warm_total_ms_p95": round(
                sorted(warm_totals)[int(0.95 * (len(warm_totals) - 1))], 2
            ),
            "warm_db_ms_median": round(statistics.median(warm_db), 2),
            "self_retrieval_found": found,
            "self_retrieval_checked": len(self_checks),
        },
        "queries": results,
        "self_retrieval": self_checks,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
