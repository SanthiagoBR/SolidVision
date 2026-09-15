"""What does a capture-date range do to the HNSW scan? Measure, do not deduce.

RFC-028 section 8.1 argues that date is a harder filter than device for an
approximate vector index: its selectivity is whatever interval the user asks
for, and there is no finite set of ranges to build one partial HNSW index per.
The regime to fear is the middle one -- a range wide enough that the planner
keeps the index, narrow enough that post-filtering the `ef_search` candidates
discards most of them -- which shows up as **fewer than `limit` rows, with no
error at all**. A script that only timed queries would never see it, so every
run below reports rows returned against rows asked for.

Starting point: `experiments/rfc-027-devices/planner_check.py`, which measured
the device filter the same way on the same corpus size.

The corpus spreads capture dates uniformly across 2000-2020 for 90% of the
images and leaves 10% with no date, so a range naming a fraction of those
twenty years has a known selectivity over the dated rows, and the unknown-date
count query of RFC-028 section 4.1 has something to count.

Each selectivity is measured against the schema as migrated (no index on
`captured_at`) and again with a B-tree on `captured_at` created inside the same
rolled-back transaction. The second run exists to inform whether the migration
should have added one -- a decision this script informs rather than RFC-028
presuming.

**Custom plans and generic plans are measured separately, on purpose.** The
first version of this script mixed them by accident: psycopg server-side
prepares a statement after five executions of the same text, and PostgreSQL
may then run it with a *generic* plan, costed without knowing the range's
bounds -- so the rows a timed query returned and the plan `EXPLAIN ANALYZE`
printed for it stopped describing the same execution. The application sends
exactly that: one SQL text for every date-filtered search, differing only in
parameters. So both are reported -- a custom plan per range (preparation
disabled), and the plan PostgreSQL uses when forced to be generic
(`plan_cache_mode = force_generic_plan`). Whether `plan_cache_mode = auto`
actually switches a long-lived pooled connection to the generic plan is not
something this script measures.

Everything happens inside one transaction that is rolled back, so the
development database is left exactly as it was found.

    backend/.venv/Scripts/python.exe experiments/rfc-028-capture-date/planner_check.py

`RFC028_CORPUS_SIZE` overrides the corpus size.
"""

from __future__ import annotations

import datetime
import os
import random
import sys
import time
import uuid
from typing import Any

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from psycopg import ClientCursor  # noqa: E402
from sqlalchemy import delete, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.infrastructure.database.models.device_model import DeviceModel  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402

random.seed(0)

CORPUS_SIZE = int(os.environ.get("RFC028_CORPUS_SIZE", 20_000))
"""Same default as RFC-027's check, so the two filters are comparable."""

UNKNOWN_FRACTION = 0.10
"""Images with `captured_at` NULL -- scans, exports that stripped EXIF."""

EPOCH_START = datetime.datetime(2000, 1, 1)
EPOCH_END = datetime.datetime(2020, 1, 1)
LIMIT = 10
SELECTIVITIES = (0.01, 0.10, 0.20, 0.25, 0.30, 0.50, 0.90)
"""Fraction of the *dated* images each range covers.

1%, 10%, 50% and 90% are the points RFC-028 section 8.1 named. 20-30% were
added after the first run: a post-filtered HNSW scan explores `ef_search`
(40) candidates, so it can only come back short when fewer than
`limit / ef_search` = 25% of them survive the filter -- and none of the
four named points sat in that band while the planner was still using the
index. They are here to look for the short result where it can exist,
instead of concluding from its absence where it cannot.
"""

DEVICE_ID = uuid.UUID(int=0xD0000000)


def seeded_uuid() -> uuid.UUID:
    """A UUID from the seeded generator, so every run builds the same corpus.

    `uuid.uuid4()` is not seeded, and the HNSW graph depends on insertion
    order and ids: the first runs of this script reported 6, 7 and then 8
    rows for the same range, on corpora that differed only in their ids.
    """
    return uuid.UUID(int=random.getrandbits(128), version=4)


def unit_vector() -> str:
    values = [random.gauss(0, 1) for _ in range(512)]
    norm = sum(v * v for v in values) ** 0.5
    return "[" + ",".join(f"{v / norm:.6f}" for v in values) + "]"


def random_capture() -> datetime.datetime | None:
    if random.random() < UNKNOWN_FRACTION:
        return None
    span = (EPOCH_END - EPOCH_START).total_seconds()
    return EPOCH_START + datetime.timedelta(seconds=int(random.random() * span))


def seed(session: Session) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    session.execute(
        text(
            "INSERT INTO devices (id, volume_identity, volume_kind, label, "
            "first_seen_at, last_seen_at) VALUES (:id, :identity, "
            "'windows-volume-guid', 'HD-BENCH', :now, :now)"
        ),
        {"id": str(DEVICE_ID), "identity": "\\\\?\\Volume{bench}\\", "now": now},
    )
    written = 0
    while written < CORPUS_SIZE:
        chunk = min(500, CORPUS_SIZE - written)
        rows = []
        for _ in range(chunk):
            captured_at = random_capture()
            rows.append(
                {
                    "id": str(seeded_uuid()),
                    "device_id": str(DEVICE_ID),
                    "relative_path": f"bench/{seeded_uuid().hex}.jpg",
                    "embedding": unit_vector(),
                    "captured_at": captured_at,
                    "capture_source": (
                        "exif_original" if captured_at is not None else "unknown"
                    ),
                }
            )
        session.execute(
            text(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                "extension, embedding, captured_at, capture_source) VALUES "
                "(:id, :device_id, :relative_path, 'bench', 'jpg', :embedding, "
                ":captured_at, :capture_source)"
            ),
            rows,
        )
        written += chunk
    session.commit()


UNFILTERED = """
    SELECT id, embedding <=> :q AS distance
    FROM images
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> :q, id
    LIMIT :limit
"""

FILTERED = """
    SELECT id, embedding <=> :q AS distance
    FROM images
    WHERE embedding IS NOT NULL
      AND captured_at >= :start AND captured_at < :end
    ORDER BY embedding <=> :q, id
    LIMIT :limit
"""

UNKNOWN_COUNT = """
    SELECT count(*) FROM images
    WHERE embedding IS NOT NULL AND captured_at IS NULL
"""


def range_covering(fraction: float) -> tuple[datetime.datetime, datetime.datetime]:
    """A window starting mid-epoch that covers `fraction` of the dated rows."""
    span = EPOCH_END - EPOCH_START
    width = span * fraction
    start = EPOCH_START + (span - width) / 2
    return start, start + width


def plan_kind(plan: str) -> str:
    """Reduce a plan to the distinction RFC-027 section 9.1 draws.

    Only the HNSW branch is approximate; every other plan reaches all rows
    satisfying the filter and is exact however it got there.
    """
    if "ix_images_embedding_hnsw" in plan:
        return "hnsw index scan (approximate)"
    if "Bitmap Heap Scan" in plan or "Index Scan using ix_bench_captured_at" in plan:
        return "b-tree on captured_at (exact)"
    if "Seq Scan" in plan:
        return "sequential scan (exact)"
    return "other"


def measure(session: Session, label: str, sql: str, params: dict[str, object]) -> None:
    """Run one custom-planned query and print rows, time, and its plan.

    Rows come from the real execution and the plan from `EXPLAIN ANALYZE`
    of the same text and parameters; with server-side preparation disabled
    (see `main()`), both are planned for these exact bounds.
    """
    started = time.perf_counter()
    returned = len(session.execute(text(sql), params).all())
    elapsed_ms = (time.perf_counter() - started) * 1000
    plan = "\n".join(
        str(row[0])
        for row in session.execute(text("EXPLAIN ANALYZE " + sql), params).all()
    )
    report(label, returned, elapsed_ms, plan)


GENERIC_PREPARE = """
    PREPARE bench_generic(vector, int, timestamp, timestamp) AS
    SELECT id, embedding <=> $1 AS distance
    FROM images
    WHERE embedding IS NOT NULL AND captured_at >= $3 AND captured_at < $4
    ORDER BY embedding <=> $1, id
    LIMIT $2
"""

GENERIC_EXECUTE = "EXECUTE bench_generic(%s::vector, %s, %s, %s)"


def measure_generic(
    cursor: ClientCursor[Any], label: str, params: tuple[object, ...]
) -> None:
    """The same filtered query, executed through a generic prepared plan.

    A client-side binding cursor, because `PREPARE ... $1` sent through
    psycopg's extended protocol has its `$n` read as protocol parameters,
    which PostgreSQL then cannot type.
    """
    started = time.perf_counter()
    cursor.execute(GENERIC_EXECUTE, params)
    returned = len(cursor.fetchall())
    elapsed_ms = (time.perf_counter() - started) * 1000
    cursor.execute("EXPLAIN ANALYZE " + GENERIC_EXECUTE, params)
    plan = "\n".join(str(row[0]) for row in cursor.fetchall())
    report(label, returned, elapsed_ms, plan)


def vacuum_images() -> None:
    """Clear dead tuples left by earlier runs before measuring anything.

    Every run inserts the whole corpus and rolls it back, which leaves that
    many dead tuples in the heap and in the HNSW graph until autovacuum
    gets to them. Measured on this machine: after four back-to-back runs,
    even the *unfiltered* query abandoned the index for a 240 ms
    sequential scan -- RFC-025 section 7.3's bloat, produced by the
    benchmark itself. `VACUUM` removes dead tuples only; with the table
    empty of live rows, as the development database is, it changes no data.
    """
    with EngineInstance.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as autocommit:
        autocommit.execute(text("VACUUM (ANALYZE) images"))


def report(label: str, returned: int, elapsed_ms: float, plan: str) -> None:
    short = "" if returned == LIMIT else f"  <-- SHORT by {LIMIT - returned}"
    print(
        f"{label:>24}  rows={returned:2d}/{LIMIT}  {elapsed_ms:7.1f} ms  "
        f"{plan_kind(plan)}{short}"
    )
    print("      " + plan.replace("\n", "\n      "))
    print()


def exact_matches(
    session: Session, start: datetime.datetime, end: datetime.datetime
) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM images WHERE embedding IS NOT NULL "
                "AND captured_at >= :start AND captured_at < :end"
            ),
            {"start": start, "end": end},
        ).scalar_one()
    )


def run_all(session: Session, query: str, heading: str) -> None:
    print(f"===== {heading}: custom plans =====")
    print()
    measure(session, "unfiltered", UNFILTERED, {"q": query, "limit": LIMIT})
    for fraction in SELECTIVITIES:
        start, end = range_covering(fraction)
        matching = exact_matches(session, start, end)
        measure(
            session,
            f"{fraction:>4.0%} ({matching} rows)",
            FILTERED,
            {"q": query, "limit": LIMIT, "start": start, "end": end},
        )

    print(f"===== {heading}: forced generic plan =====")
    print()
    driver = session.connection().connection.driver_connection
    with ClientCursor(driver) as cursor:  # type: ignore[arg-type]
        cursor.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cursor.execute(GENERIC_PREPARE)
        for fraction in SELECTIVITIES:
            start, end = range_covering(fraction)
            measure_generic(
                cursor, f"{fraction:>4.0%} generic", (query, LIMIT, start, end)
            )
        cursor.execute("DEALLOCATE bench_generic")
        cursor.execute("SET LOCAL plan_cache_mode = auto")

    started = time.perf_counter()
    unknown = session.execute(text(UNKNOWN_COUNT)).scalar_one()
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"unknown-date count query: {unknown} rows counted in {elapsed_ms:.1f} ms")
    plan = "\n".join(
        str(row[0])
        for row in session.execute(text("EXPLAIN ANALYZE " + UNKNOWN_COUNT)).all()
    )
    print("      " + plan.replace("\n", "\n      "))
    print()


def main() -> None:
    vacuum_images()
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    query = unit_vector()
    # Never server-side prepare: every custom-plan measurement must be planned
    # for its own bounds. Generic plans are measured explicitly instead.
    driver = connection.connection.driver_connection
    driver.prepare_threshold = None  # type: ignore[union-attr]

    try:
        session.execute(delete(ImageModel))
        session.execute(delete(DeviceModel))
        session.commit()

        print(
            f"seeding {CORPUS_SIZE} images, {UNKNOWN_FRACTION:.0%} with no capture "
            f"date, the rest uniform over {EPOCH_START.year}-{EPOCH_END.year}..."
        )
        started = time.perf_counter()
        seed(session)
        session.execute(text("ANALYZE images"))
        print(f"seeded in {time.perf_counter() - started:.0f} s")
        ef_search = session.execute(text("SHOW hnsw.ef_search")).scalar_one()
        version = session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        print(f"pgvector {version}, hnsw.ef_search = {ef_search}, limit = {LIMIT}")
        print()

        run_all(session, query, "schema as migrated: no index on captured_at")

        session.execute(
            text("CREATE INDEX ix_bench_captured_at ON images (captured_at)")
        )
        session.execute(text("ANALYZE images"))
        run_all(session, query, "with a b-tree on captured_at (rolled back)")
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        print("rolled back; the development database is unchanged")


if __name__ == "__main__":
    main()
