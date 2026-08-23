"""RFC-026 end-to-end check: does the real HTTP API actually work?

Run this after pulling the branch, with Postgres up and migrations
applied, and it will tell you PASS or FAIL -- no pytest, no markers, no
memorizing which of the three test levels to run. See README.md in this
directory for the one-time setup.

What it does, in order:

  1. Materializes the RFC-022 demo corpus (45 images) into a temp
     directory and indexes it with the real CLIP model, inside a
     database transaction that is rolled back at the very end -- your
     database is read from, but nothing written here survives.
  2. Starts the real FastAPI app under a real uvicorn server on a real
     socket (127.0.0.1:8127), pointed at that indexed corpus.
  3. Sends real HTTP requests at it -- /health, an English search, a
     Portuguese search, and the three documented error cases -- and
     checks the response against what RFC-026 promises.
  4. Prints one line per check and a final PASS/FAIL summary, and exits
     1 if anything failed (so this is CI-friendly too, if you want it).

First run downloads two Hugging Face checkpoints (~5 s CLIP + ~5 s Marian
the first time either is needed) -- that is expected, not a hang.

    cd SolidVision
    python experiments/rfc-026-search-api/friend_e2e_check.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

HOST = "127.0.0.1"
PORT = 8127
BASE_URL = f"http://{HOST}:{PORT}"

CHECKS_RUN: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one pass/fail line and print it immediately.

    Deliberately does not raise: one failed check should not stop the
    rest from running, so the friend sees every problem in a single pass
    instead of fixing them one at a time across five reruns.
    """
    status = "PASS" if condition else "FAIL"
    line = f"  [{status}] {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    CHECKS_RUN.append((name, condition, detail))


def try_imports() -> None:
    """Fail with a readable message instead of a raw traceback.

    The single most likely first failure for a friend running this cold
    is dependencies not installed -- catching it here means the very
    first thing printed is actionable.
    """
    try:
        import httpx  # noqa: F401
        import uvicorn  # noqa: F401
        from sqlalchemy import delete  # noqa: F401
    except ImportError as exc:
        print(f"Missing a dependency: {exc}")
        print("Run: pip install -r backend/requirements.txt")
        sys.exit(1)


def main() -> int:
    print("=" * 72)
    print("RFC-026 Search API -- end-to-end check")
    print("=" * 72)

    try_imports()

    import httpx
    import uvicorn
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    from app.application.use_cases.index_or_update_images import (
        IndexOrUpdateImagesUseCase,
    )
    from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
    from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
    from app.infrastructure.database.models.image_model import ImageModel
    from app.infrastructure.filesystem.filesystem_image_provider import (
        FilesystemImageProvider,
    )
    from app.infrastructure.filesystem.sha256_content_hasher import (
        Sha256ContentHasher,
    )
    from app.infrastructure.persistence.engine import EngineInstance
    from app.infrastructure.persistence.postgres_image_repository import (
        PostgresImageRepository,
    )
    from app.infrastructure.workers.indexing_worker import IndexingWorker
    from app.presentation.api import app
    from app.presentation.dependencies import (
        get_embedding_model,
        get_image_repository,
    )
    from dataset_tools.manifest import (
        DEMO_CORPUS_ROOT,
        DEMO_MANIFEST_PATH,
        load_manifest,
    )
    from dataset_tools.materialize import materialize

    print("\n1. Checking the database connection...")
    try:
        connection = EngineInstance.connect()
    except Exception as exc:  # noqa: BLE001 -- friendly first-failure message
        print(f"  Could not reach PostgreSQL: {exc}")
        print("  Is `docker compose up -d` running, and is .env correct?")
        return 1
    check("PostgreSQL is reachable", True)

    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    try:
        print("\n2. Indexing the demo corpus (rolled back afterwards, nothing kept)...")
        session.execute(delete(ImageModel))
        session.commit()

        manifest = load_manifest(DEMO_MANIFEST_PATH)
        manifest.verify_matches_directory(DEMO_CORPUS_ROOT)

        with tempfile.TemporaryDirectory() as tmp:
            root = materialize(manifest, DEMO_CORPUS_ROOT, Path(tmp) / "demo")

            print("   Loading CLIP (first run downloads it -- can take a minute)...")
            embedding_model = ClipEmbeddingModel()
            repository = PostgresImageRepository(session)

            summary = IndexingWorker(
                filesystem_provider=FilesystemImageProvider(
                    root, SUPPORTED_IMAGE_EXTENSIONS
                ),
                index_or_update_images_use_case=IndexOrUpdateImagesUseCase(
                    repository=repository,
                    embedding_model=embedding_model,
                    content_hasher=Sha256ContentHasher(),
                    batch_size=8,
                    metadata_prefetch_size=512,
                ),
            ).run()

        check(
            f"Indexed the 45-image demo corpus ({summary.indexed} indexed, "
            f"{len(summary.failures)} failed)",
            summary.indexed == 45 and not summary.failures,
            f"summary={summary}",
        )

        print("\n3. Starting the real API server on a real socket...")
        app.dependency_overrides[get_image_repository] = lambda: repository
        app.dependency_overrides[get_embedding_model] = lambda: embedding_model

        config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.1)
        check("The server started", server.started)

        if not server.started:
            print("  Server never came up -- is port 8127 already in use?")
        else:
            with httpx.Client(timeout=30.0) as http:
                print("\n4. Checking /health...")
                health = http.get(f"{BASE_URL}/health")
                check("GET /health -> 200", health.status_code == 200, health.text)
                check(
                    "GET /health reports the database connected",
                    health.status_code == 200
                    and health.json().get("database") == "connected",
                    health.text,
                )

                print("\n5. Checking a real English search...")
                english = http.get(
                    f"{BASE_URL}/api/v1/images/search",
                    params={
                        "q": "artificial fish farming ponds in a valley",
                        "limit": 5,
                    },
                )
                check(
                    "English search -> 200",
                    english.status_code == 200,
                    english.text,
                )
                if english.status_code == 200:
                    body = english.json()
                    filenames = {r["filename"] for r in body["results"]}
                    check(
                        "response has query, limit, and results",
                        {"query", "limit", "results"} <= set(body),
                        str(body)[:200],
                    )
                    check(
                        "a relevant image (a fish pond) is in the top 5",
                        any("fish_ponds" in name for name in filenames),
                        f"got {sorted(filenames)}",
                    )
                    check(
                        "no result exposes a server filesystem path",
                        all("path" not in r for r in body["results"]),
                        str(body["results"])[:200],
                    )
                    check(
                        "every similarity is a float in [-1, 1]",
                        all(
                            isinstance(r["similarity"], float)
                            and -1.0 <= r["similarity"] <= 1.0
                            for r in body["results"]
                        ),
                        str([r["similarity"] for r in body["results"]]),
                    )

                print("\n6. Checking a Portuguese search is translated...")
                portuguese = http.get(
                    f"{BASE_URL}/api/v1/images/search",
                    params={"q": "uma propriedade rural com um lago", "limit": 5},
                )
                check(
                    "Portuguese search -> 200",
                    portuguese.status_code == 200,
                    portuguese.text,
                )
                if portuguese.status_code == 200 and english.status_code == 200:
                    pt_ids = {r["id"] for r in portuguese.json()["results"]}
                    en_lake_query = http.get(
                        f"{BASE_URL}/api/v1/images/search",
                        params={"q": "rural property with a lake", "limit": 5},
                    ).json()
                    en_ids = {r["id"] for r in en_lake_query["results"]}
                    check(
                        "Portuguese and its English equivalent overlap in the top 5"
                        " (proves translation ran, not just CLIP)",
                        bool(pt_ids & en_ids),
                        f"pt={pt_ids} en={en_ids}",
                    )
                    check(
                        "the response echoes the Portuguese query verbatim,"
                        " not the English prompt CLIP saw",
                        portuguese.json()["query"]
                        == "uma propriedade rural com um lago",
                        portuguese.json().get("query"),
                    )

                print("\n7. Checking the documented error cases...")
                blank = http.get(
                    f"{BASE_URL}/api/v1/images/search", params={"q": "   "}
                )
                check("blank query -> 400", blank.status_code == 400, blank.text)

                too_big = http.get(
                    f"{BASE_URL}/api/v1/images/search",
                    params={"q": "x", "limit": 101},
                )
                check("limit=101 -> 400", too_big.status_code == 400, too_big.text)

                missing = http.get(f"{BASE_URL}/api/v1/images/search")
                check("missing q -> 422", missing.status_code == 422, missing.text)

        server.should_exit = True
        server_thread.join(timeout=10)
    except Exception:  # noqa: BLE001 -- show the friend the real traceback too
        print("\nAn unexpected error interrupted the check:\n")
        traceback.print_exc()
        return 1
    finally:
        app.dependency_overrides.clear()
        session.close()
        transaction.rollback()
        connection.close()

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in CHECKS_RUN if ok)
    total = len(CHECKS_RUN)
    if passed == total:
        print(f"ALL {total} CHECKS PASSED")
        print("=" * 72)
        return 0

    print(f"{total - passed} OF {total} CHECKS FAILED")
    for name, ok, detail in CHECKS_RUN:
        if not ok:
            print(f"  FAILED: {name}" + (f" ({detail})" if detail else ""))
    print("=" * 72)
    return 1


if __name__ == "__main__":
    sys.exit(main())
