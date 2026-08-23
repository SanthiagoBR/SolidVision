"""RFC-026 indexing + query timing check: real data, real Postgres, real numbers.

Unlike `friend_e2e_check.py`, this one **writes for real** -- it indexes
the 45-image RFC-022 demo corpus straight from `backend/dataset/demo/` into
your actual PostgreSQL database (an upsert: safe to re-run, existing rows
just get refreshed) and leaves it there, so you end up with a real,
searchable, browsable corpus afterwards rather than a corpus that vanishes
when the script exits.

What it measures, per RFC-026's own §12 discipline -- print the real number,
not an assumption:

  1. **Time per image indexed** -- one line per image, split into the CLIP
     encode and the PostgreSQL write, because RFC-024 measured those as two
     very different costs (encode dominates; persistence is ~1-2% of a run).
  2. **Time per query, English** -- runs all 25 RFC-022 ground-truth queries
     through the real search stack, checks whether the expected image lands
     in the top 5 (recall), and times each one.
  3. **Time per query, Portuguese, split into translation and search** --
     the same 25 queries, hand-translated to Portuguese (verbatim from the
     RFC-023 SigLIP bake-off's `PORTUGUESE_TRANSLATIONS`, so a friend
     comparing this run against that one is comparing the same sentences).
     `ClipEmbeddingModel.build_prompt()` is timed on its own first -- that
     is exactly `detect -> translate -> template`, nothing else -- so the
     translation cost is visible instead of buried inside a single "search
     took 400ms" number. The full search is then timed separately (and
     necessarily re-runs the translation internally; there is no public API
     to hand `encode_text` an already-built prompt, and reaching into the
     adapter's private methods to avoid that would be worse than the extra
     few hundred milliseconds it costs a one-off benchmark script).

First run downloads two Hugging Face checkpoints (~5 s CLIP + ~5 s Marian
the first time a Portuguese string is translated) -- expected, not a hang.

    cd SolidVision
    python experiments/rfc-026-search-api/friend_indexing_timing_check.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

# Hand-written Portuguese translations of the 25 committed English queries,
# copied verbatim from `experiments/rfc-023-siglip-bakeoff/siglip_bakeoff.py`
# on the `explore/siglip-bakeoff` branch -- the same sentences a friend
# running that bake-off already saw, so numbers from the two scripts are
# comparable rather than measuring two different query sets. Keyed by the
# exact English string in `queries.json` so a mismatch (a query edited
# later) fails loudly instead of silently skipping the PT half.
PORTUGUESE_TRANSLATIONS: dict[str, str] = {
    "rural property with a small lake": "propriedade rural com um pequeno lago",
    "small natural lake on a farm, not an artificial pond": (
        "pequeno lago natural em uma fazenda, nao um tanque artificial"
    ),
    "hillside property with a swimming pool": "propriedade em encosta com piscina",
    "swimming pool at a rural home, not a natural or artificial water body": (
        "piscina em uma casa rural, nao um corpo d'agua natural ou artificial"
    ),
    "artificial fish farming ponds in a valley": (
        "tanques artificiais de piscicultura em um vale"
    ),
    "aquaculture ponds seen from above, not a swimming pool or lake": (
        "tanques de piscicultura vistos de cima, nao uma piscina ou lago"
    ),
    "industrial warehouse beside farmland": (
        "galpao industrial ao lado de uma area agricola"
    ),
    "warehouse next to a highway, not a warehouse inside a town": (
        "galpao ao lado de uma rodovia, nao um galpao dentro de uma cidade"
    ),
    "cleared construction lot with exposed red soil": (
        "terreno desmatado com solo vermelho exposto"
    ),
    "recently cleared building site with terraced red dirt": (
        "terreno recem-desmatado com terraplenagem em solo vermelho"
    ),
    "town intersection with a car dealership": (
        "cruzamento urbano com uma concessionaria de carros"
    ),
    "car dealership canopy with cars for sale, not a parking lot on a plain street": (
        "cobertura de concessionaria com carros a venda, nao um "
        "estacionamento em uma rua comum"
    ),
    "commercial street with shops and storefronts": (
        "rua comercial com lojas e vitrines"
    ),
    "workshops and small businesses lining a commercial street": (
        "oficinas e pequenos comercios ao longo de uma rua comercial"
    ),
    "low-altitude view of storefronts with pedestrians and parked cars": (
        "vista de baixa altitude de vitrines com pedestres e carros estacionados"
    ),
    "residential street lined with houses": "rua residencial ladeada de casas",
    "quiet neighborhood street with family houses and a workshop": (
        "rua residencial tranquila com casas de familia e uma oficina"
    ),
    "hillside town overview with dense houses": (
        "vista geral de uma cidade na encosta com casas densas"
    ),
    "suburban town spread across rolling hills in a loose grid": (
        "cidade suburbana espalhada por colinas suaves em uma malha irregular"
    ),
    "wide drone overview of a hillside town with a large building complex": (
        "vista aerea ampla de uma cidade na encosta com um grande complexo de edificios"
    ),
    "a black cat resting indoors": "um gato preto descansando dentro de casa",
    "a dog looking directly at the camera": (
        "um cachorro olhando diretamente para a camera"
    ),
    "a bicycle parked on a street": "uma bicicleta estacionada em uma rua",
    "a ceramic coffee cup and saucer": "uma xicara de cafe de ceramica com pires",
    "a person working on a laptop indoors": (
        "uma pessoa trabalhando em um laptop dentro de casa"
    ),
}

# RFC-025 section 11's measured floor, not a number invented here: a query
# missing its relevant image in the top 5 is expected noise up to this
# rate on a 512-dim general-purpose checkpoint, not a broken pipeline. See
# tests/dataset/test_semantic_search_e2e.py for the full argument.
RECALL_AT_5_FLOOR = 0.76


def fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:7.1f} ms"


def stats_line(label: str, samples: list[float]) -> str:
    if not samples:
        return f"  {label}: no samples"
    return (
        f"  {label:<28} mean {fmt_ms(statistics.mean(samples))}"
        f"   median {fmt_ms(statistics.median(samples))}"
        f"   min {fmt_ms(min(samples))}"
        f"   max {fmt_ms(max(samples))}"
    )


def main() -> int:
    print("=" * 78)
    print("RFC-026 Search API -- indexing + query timing check")
    print("=" * 78)

    try:
        from sqlalchemy import text

        from app.application.use_cases.search_images import SearchImagesUseCase
        from app.domain.entities.image import Image
        from app.domain.value_objects.image_path import ImagePath
        from app.domain.value_objects.indexing_record import IndexingRecord
        from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
        from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
        from app.infrastructure.filesystem.filesystem_image_provider import (
            FilesystemImageProvider,
        )
        from app.infrastructure.filesystem.image_identity import compute_image_id
        from app.infrastructure.persistence.postgres_image_repository import (
            PostgresImageRepository,
        )
        from app.infrastructure.persistence.session import SessionLocal
        from dataset_tools.manifest import (
            DEMO_CORPUS_ROOT,
            DEMO_MANIFEST_PATH,
            load_manifest,
        )
    except ImportError as exc:
        print(f"Missing a dependency: {exc}")
        print("Run: pip install -r backend/requirements.txt")
        return 1

    print("\n1. Checking the database connection...")
    try:
        session = SessionLocal()
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 -- friendly first-failure message
        print(f"  Could not reach PostgreSQL: {exc}")
        print("  Is `docker compose up -d` running, and is .env correct?")
        return 1
    print("  PostgreSQL is reachable.")

    print("\n2. Checking the demo corpus is intact...")
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    print(f"  {len(manifest.images)} images described, all present.")

    print("\n3. Loading CLIP (first run downloads it -- can take a minute)...")
    embedding_model = ClipEmbeddingModel()
    repository = PostgresImageRepository(session)

    print("\n4. Indexing all 45 images into PostgreSQL (this is a real write,")
    print("   not a rollback -- re-running this script safely re-indexes")
    print("   the same rows).\n")

    discovered = sorted(
        FilesystemImageProvider(
            DEMO_CORPUS_ROOT, SUPPORTED_IMAGE_EXTENSIONS
        ).discover(),
        key=lambda d: d.path,
    )
    encode_times: list[float] = []
    persist_times: list[float] = []
    failures: list[tuple[str, Exception]] = []

    for i, discovered_file in enumerate(discovered, start=1):
        image_path = ImagePath(str(discovered_file.path))
        image = Image(
            id=compute_image_id(image_path),
            path=image_path,
            filename=discovered_file.filename,
            extension=discovered_file.extension,
        )
        try:
            encode_start = time.perf_counter()
            embedding = embedding_model.encode_image(image)
            encode_seconds = time.perf_counter() - encode_start

            persist_start = time.perf_counter()
            repository.save_indexed(
                IndexingRecord(
                    image=image,
                    embedding=embedding,
                    file_size=discovered_file.file_size,
                    file_modified_at=discovered_file.file_modified_at,
                )
            )
            persist_seconds = time.perf_counter() - persist_start
        except Exception as exc:  # noqa: BLE001 -- one bad file must not end the run
            failures.append((discovered_file.filename, exc))
            print(f"  [{i:2d}/45] FAILED  {discovered_file.filename:<28} {exc}")
            continue

        encode_times.append(encode_seconds)
        persist_times.append(persist_seconds)
        print(
            f"  [{i:2d}/45] {discovered_file.filename:<28}"
            f" encode {fmt_ms(encode_seconds)}   persist {fmt_ms(persist_seconds)}"
        )

    print(f"\n  Indexed {len(encode_times)}/45, {len(failures)} failed.")
    print(stats_line("encode time / image", encode_times))
    print(stats_line("persist time / image", persist_times))
    print(f"  total indexing wall time: {sum(encode_times) + sum(persist_times):.2f} s")

    print("\n5. Running the 25 English ground-truth queries...")
    search_use_case = SearchImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        default_limit=5,
    )
    queries = json.loads(
        (DEMO_MANIFEST_PATH.parent / "queries.json").read_text(encoding="utf-8")
    )["queries"]

    en_search_times: list[float] = []
    en_hits = 0
    print()
    for entry in queries:
        query, relevant = entry["query"], set(entry["relevant"])
        started = time.perf_counter()
        hits = search_use_case.execute(query, limit=5)
        elapsed = time.perf_counter() - started
        en_search_times.append(elapsed)

        # `relevant` records repository-relative paths ("images/..."); the
        # search returns filenames without extension or directory, so
        # match on the filename stem, same as `test_search_api_e2e.py`.
        returned_stems = {Path(hit.image.filename).stem for hit in hits}
        relevant_stems = {Path(p).stem for p in relevant}
        found = bool(returned_stems & relevant_stems)
        en_hits += found
        top_similarity = hits[0].similarity if hits else float("nan")
        status = "hit " if found else "MISS"
        print(
            f"  [{status}] {fmt_ms(elapsed)}  top1_sim={top_similarity:+.4f}"
            f"  {query[:60]}"
        )

    en_recall = en_hits / len(queries)
    print(f"\n  EN Recall@5: {en_recall:.1%} ({en_hits}/{len(queries)})")
    print(stats_line("EN search time / query", en_search_times))

    print("\n6. Running the same 25 queries in Portuguese, translation timed")
    print("   separately from the full search...\n")

    missing_translations = [
        q["query"] for q in queries if q["query"] not in PORTUGUESE_TRANSLATIONS
    ]
    if missing_translations:
        print(
            f"  WARNING: no PT translation for {len(missing_translations)} "
            "quer(ies), skipping them"
        )

    translation_times: list[float] = []
    pt_search_times: list[float] = []
    pt_hits = 0
    pt_total = 0
    for entry in queries:
        pt_query = PORTUGUESE_TRANSLATIONS.get(entry["query"])
        if pt_query is None:
            continue
        pt_total += 1
        relevant = set(entry["relevant"])

        translate_started = time.perf_counter()
        prompt = embedding_model.build_prompt(pt_query)
        translation_seconds = time.perf_counter() - translate_started
        translation_times.append(translation_seconds)

        search_started = time.perf_counter()
        hits = search_use_case.execute(pt_query, limit=5)
        search_seconds = time.perf_counter() - search_started
        pt_search_times.append(search_seconds)

        returned_stems = {Path(hit.image.filename).stem for hit in hits}
        relevant_stems = {Path(p).stem for p in relevant}
        found = bool(returned_stems & relevant_stems)
        pt_hits += found
        status = "hit " if found else "MISS"
        print(
            f"  [{status}] translate {fmt_ms(translation_seconds)}"
            f"  full-search {fmt_ms(search_seconds)}"
            f"  '{pt_query[:45]}' -> '{prompt[:55]}'"
        )

    pt_recall = pt_hits / pt_total if pt_total else 0.0
    print(f"\n  PT Recall@5: {pt_recall:.1%} ({pt_hits}/{pt_total})")
    print(stats_line("translation time / query", translation_times))
    print(stats_line("PT full search time / query", pt_search_times))

    session.close()

    print("\n" + "=" * 78)
    ok = (
        len(encode_times) == 45
        and not failures
        and en_recall >= RECALL_AT_5_FLOOR
        and pt_recall >= RECALL_AT_5_FLOOR
    )
    if ok:
        print("PASS -- all 45 images indexed, both recall floors met (>= 76%)")
        print("=" * 78)
        return 0

    print("FAIL -- see above for which stage came up short:")
    if len(encode_times) != 45 or failures:
        print(f"  indexing: {len(encode_times)}/45 indexed, {len(failures)} failed")
    if en_recall < RECALL_AT_5_FLOOR:
        print(f"  EN recall {en_recall:.1%} below the {RECALL_AT_5_FLOOR:.0%} floor")
    if pt_recall < RECALL_AT_5_FLOOR:
        print(f"  PT recall {pt_recall:.1%} below the {RECALL_AT_5_FLOOR:.0%} floor")
    print("=" * 78)
    return 1


if __name__ == "__main__":
    sys.exit(main())
