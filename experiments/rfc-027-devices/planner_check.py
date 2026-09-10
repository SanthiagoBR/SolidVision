"""What does a device filter do to the HNSW scan? Measure, do not deduce.

RFC-027 section 9.1 names three plausible regimes and refuses to pick one
from an armchair:

    very selective   -- one disk of twenty; the planner may abandon the
                        index and scan the subset, which is fast *and*
                        exact
    barely selective -- nineteen of twenty; index scan plus a post-filter
                        that discards almost nothing
    in between       -- index scan plus a post-filter that discards enough
                        of the `ef_search` candidates to return FEWER THAN
                        `limit` rows while more matching rows exist

The middle band is the real risk, and it is the same mechanism RFC-025
section 7.3 measured by accident with dead tuples: PostgreSQL does not
push an arbitrary predicate into the graph traversal, so filtering happens
to whatever the index already returned.

This script builds a synthetic corpus spread across twenty devices, runs
the shipped query under `EXPLAIN ANALYZE` at several selectivities, and
reports two things per run: which plan the planner chose, and how many
rows came back against the ten that were asked for. Everything happens
inside one transaction that is rolled back, so the development database is
left exactly as it was found.

Run it with the backend virtualenv:

    backend/.venv/Scripts/python.exe experiments/rfc-027-devices/planner_check.py
"""

from __future__ import annotations

import datetime
import os
import random
import sys
import time
import uuid

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from sqlalchemy import delete, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.infrastructure.database.models.device_model import DeviceModel  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402

random.seed(0)

DEVICE_COUNT = 20
"""Twenty external disks: the acquisition ARCHITECTURE.md section 2 describes.

The count matters to the conclusion. A filter over twenty devices is
selective at 1/20 and barely selective at 19/20, and partial HNSW indexes
per device are only a credible mitigation because this number is small and
stable -- which is exactly what a filter by *date* would not be.
"""

CORPUS_SIZE = int(os.environ.get("RFC027_CORPUS_SIZE", 20_000))
"""Overridable, because the answer depends on it.

The regime the RFC worries about opens only when a sequential scan over
the filtered subset stops looking cheap to the planner, which is a
function of table size. Measuring one corpus and generalising would be
the deduction section 9.1 refuses to make.
"""

LIMIT = 10
SELECTIVITIES = (1, 2, 5, 10, 15, 19, 20)
"""How many of the twenty devices each filtered run names."""


def unit_vector() -> str:
    values = [random.gauss(0, 1) for _ in range(512)]
    norm = sum(v * v for v in values) ** 0.5
    return "[" + ",".join(f"{v / norm:.6f}" for v in values) + "]"


def device_id(index: int) -> uuid.UUID:
    return uuid.UUID(int=0xD0000000 + index)


def seed_devices(session: Session) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    session.execute(
        text(
            "INSERT INTO devices (id, volume_identity, volume_kind, label, "
            "first_seen_at, last_seen_at) VALUES (:id, :identity, "
            "'windows-volume-guid', :label, :now, :now)"
        ),
        [
            {
                "id": str(device_id(index)),
                "identity": f"\\\\?\\Volume{{{uuid.UUID(int=index)}}}\\",
                "label": f"HD{index}",
                "now": now,
            }
            for index in range(DEVICE_COUNT)
        ],
    )
    session.commit()


def seed_images(session: Session) -> None:
    """Spread the corpus evenly across the devices.

    Even spread is the honest worst case for the middle band: with the
    images clustered on one disk, a filter naming that disk would be
    trivially unselective and a filter naming any other would be trivially
    empty, and neither measures the regime the RFC is worried about.
    """
    written = 0
    while written < CORPUS_SIZE:
        chunk = min(500, CORPUS_SIZE - written)
        session.execute(
            text(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                "extension, embedding) VALUES (:id, :device_id, :relative_path, "
                "'bench', 'png', :embedding)"
            ),
            [
                {
                    "id": str(uuid.uuid4()),
                    "device_id": str(device_id((written + offset) % DEVICE_COUNT)),
                    "relative_path": f"bench/{uuid.uuid4().hex}.png",
                    "embedding": unit_vector(),
                }
                for offset in range(chunk)
            ],
        )
        written += chunk
    session.commit()


UNFILTERED = """
    SELECT id, device_id, relative_path, embedding <=> :q AS distance
    FROM images
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> :q, id
    LIMIT :limit
"""

FILTERED = """
    SELECT id, device_id, relative_path, embedding <=> :q AS distance
    FROM images
    WHERE embedding IS NOT NULL AND device_id = ANY(:devices)
    ORDER BY embedding <=> :q, id
    LIMIT :limit
"""


def explain(session: Session, sql: str, params: dict[str, object]) -> str:
    rows = session.execute(text("EXPLAIN ANALYZE " + sql), params).all()
    return "\n".join(str(row[0]) for row in rows)


def plan_kind(plan: str) -> str:
    """Reduce a plan to the distinction the RFC section 9.1 table makes.

    Only the HNSW branch is approximate. Both of the others reach every
    row that satisfies the filter and are therefore exact, however they
    got there -- which is the property that decides whether a short result
    is possible, and the reason the bitmap case is reported separately
    rather than folded into "other".
    """
    if "ix_images_embedding_hnsw" in plan:
        return "hnsw index scan (approximate)"
    if "Bitmap Heap Scan" in plan:
        return "bitmap scan on the device index (exact)"
    if "Seq Scan" in plan:
        return "sequential scan (exact)"
    return "other"


def measure(session: Session, query: str, devices: list[str] | None) -> None:
    sql = UNFILTERED if devices is None else FILTERED
    params: dict[str, object] = {"q": query, "limit": LIMIT}
    if devices is not None:
        params["devices"] = devices

    started = time.perf_counter()
    returned = len(session.execute(text(sql), params).all())
    elapsed_ms = (time.perf_counter() - started) * 1000
    plan = explain(session, sql, params)

    label = "unfiltered" if devices is None else f"{len(devices):2d}/20 devices"
    short = "" if returned == LIMIT else f"  <-- SHORT by {LIMIT - returned}"
    print(
        f"{label:>16}  rows={returned:2d}/{LIMIT}  {elapsed_ms:7.1f} ms  "
        f"{plan_kind(plan)}{short}"
    )
    print("      " + plan.replace("\n", "\n      "))
    print()


def main() -> None:
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    query = unit_vector()

    try:
        session.execute(delete(ImageModel))
        session.execute(delete(DeviceModel))
        session.commit()

        print(f"seeding {DEVICE_COUNT} devices and {CORPUS_SIZE} images...")
        seed_devices(session)
        seed_images(session)
        session.execute(text("ANALYZE images"))
        print()

        ef_search = session.execute(text("SHOW hnsw.ef_search")).scalar_one()
        print(f"hnsw.ef_search = {ef_search}")
        print()

        measure(session, query, devices=None)
        for count in SELECTIVITIES:
            measure(
                session,
                query,
                devices=[str(device_id(index)) for index in range(count)],
            )
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        print("rolled back; the development database is unchanged")


if __name__ == "__main__":
    main()
