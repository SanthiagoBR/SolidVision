"""Does the tie-break cost us the HNSW index? Measure, do not assume."""

from __future__ import annotations

import random
import sys
import time
import uuid

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from sqlalchemy import delete, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402

random.seed(0)

SIZES = (45, 2000, 10000)


def unit_vector() -> str:
    values = [random.gauss(0, 1) for _ in range(512)]
    norm = sum(v * v for v in values) ** 0.5
    return "[" + ",".join(f"{v / norm:.6f}" for v in values) + "]"


def explain(session: Session, sql: str, params: dict[str, object]) -> str:
    rows = session.execute(text("EXPLAIN ANALYZE " + sql), params).all()
    return "\n".join(str(row[0]) for row in rows)


WITH_TIEBREAK = """
    SELECT id, path, embedding <=> :q AS distance
    FROM images
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> :q, id
    LIMIT 10
"""

WITHOUT_TIEBREAK = """
    SELECT id, path, embedding <=> :q AS distance
    FROM images
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> :q
    LIMIT 10
"""


def main() -> None:
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    query = unit_vector()

    try:
        session.execute(delete(ImageModel))
        session.commit()

        current = 0
        for size in SIZES:
            started = time.perf_counter()
            while current < size:
                chunk = min(500, size - current)
                session.execute(
                    text(
                        "INSERT INTO images "
                        "(id, path, filename, extension, embedding) "
                        "VALUES (:id, :path, 'bench', 'png', :embedding)"
                    ),
                    [
                        {
                            "id": str(uuid.uuid4()),
                            "path": f"bench/{uuid.uuid4().hex}.png",
                            "embedding": unit_vector(),
                        }
                        for _ in range(chunk)
                    ],
                )
                session.commit()
                current += chunk
            session.execute(text("ANALYZE images"))
            print(f"\ninserted up to {size} rows in {time.perf_counter()-started:.1f}s")

            print("=" * 72)
            print(f"rows = {size}")
            print("-- WITH tie-break (the shipped query) --")
            print(explain(session, WITH_TIEBREAK, {"q": query}))
            print("-- WITHOUT tie-break --")
            print(explain(session, WITHOUT_TIEBREAK, {"q": query}))
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        print("\nrolled back")


if __name__ == "__main__":
    main()
