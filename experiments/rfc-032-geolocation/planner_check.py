"""What does a circle do to the HNSW scan, and what fixes it? Measure, then decide.

RFC-028 section 8.1 measured a date range on 20,000 images and confirmed the bad
regime: between 20% and 30% selectivity the planner keeps the HNSW index, the
post-filter discards candidates, and the search returns **5 to 9 rows of 10,
with no error at all**. It recorded the mitigation as an open risk. RFC-032
section 6.1 argues a circle is worse -- positions are *clustered*, so a few
kilometres around a much-photographed place keeps a large fraction of the
collection, and the bad band is the common case -- and decides the mitigation
here, with the criterion stated before the number.

**The corpus is clustered, not uniform.** Uniform positions would measure a
problem this collection does not have. 90% of the images are placed around 40
sites inside the pilot's bounding box (RFC-032 section 2.3), with Zipf weights
(s = 0.85) so that the five busiest ~100 m cells hold close to the pilot's 45%,
and 60 m of scatter per site; 10% have no position. The circle is centred on
the busiest site, and its radius is searched for until it covers each target
fraction of the corpus -- as closely as the clusters allow, and the achieved
fraction is what is printed.

**The query is the application's.** Every search below executes
`PostgresImageRepository.search_statement()` -- bounding-box pre-filter,
haversine, ordering and all -- not an imitation of it.

**Latency is the server's planning plus execution time**, read from
`EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON)` of the same statement, and that
is a correction made after the first run rather than the plan going in. The
first run timed the round trip and found a flat ~47 ms under *every* search,
including an unfiltered HNSW scan RFC-028 had measured at 4.9 ms. The cause is
the development transport, not PostgreSQL: on this machine any message over
~8 KB to the Docker-published port costs +43 ms (measured: 8,000 bytes 1.9 ms,
9,000 bytes 44 ms), and a 512-float query vector serialized by pgvector is
~10 KB. That constant would compress every filtered/unfiltered ratio toward 1
and let the 2x criterion pass for any plan at all. Wall-clock p95 is still
printed beside it, labelled.

**About "45% in the 5 busiest cells".** The pilot's statistic is not
scale-invariant: 40 photos can fill only a handful of ~100 m cells, 18,000
cannot. What the corpus has to reproduce is the property the statistic was
standing in for -- a large share of the collection packed into a small area
-- and the line printed after seeding states it in terms that survive the
change of scale: the share of the five busiest *sites*, and the radius that
already holds 10% of everything.

Three configurations (section 6.1, item 2), each with and without a spatial
index -- a B-tree on `latitude`, or a GiST on `point(longitude, latitude)`,
which needs no extension and is reached by adding a `<@ box` clause:

    baseline        -- what RFC-025 shipped
    strict_order    -- SET LOCAL hnsw.iterative_scan = strict_order
    ef_search=N     -- SET LOCAL hnsw.ef_search = N, for N in 100, 200, 400

measured with custom plans (one plan per circle) and with a forced generic plan
(one plan for every circle, which is what a long-lived connection may settle
on), each over `QUERIES` random query vectors.

**Criteria, declared before the numbers (RFC-032 section 6.1, items 3 and 4):**

- *Mitigation:* the cheapest configuration that returns `limit` of `limit` for
  every query, at every measured selectivity, under both plan kinds, with the
  server-side p95 of the filtered search **no worse than 2x the server-side
  p95 of the unfiltered search**. "Cheapest" is the lowest worst-case filtered
  p95. If none passes, that is the result, recorded as such.
- *Spatial index:* created only if it changes the verdict (a configuration
  passes with it and not without), or halves the chosen configuration's worst
  filtered p95, or halves the map's whole-collection query at 100,000 rows.
- *`MAX_MAP_CELLS`:* the largest of 500, 1000, 2000 and 5000 whose worst-case
  answer -- that many cells, at the bytes per cell measured on the real
  response schema -- stays within 256 KB.

  **This is the second version of that criterion, and the first is kept in
  the record.** It read "within 256 KB of cell JSON and 250 ms of server time,
  for the whole collection at the default precision", and on the first run it
  selected nothing -- not because every ceiling was bad, but because it asked
  the wrong question twice. The time bound measured the full scan of 100,000
  rows, ~250-390 ms *whatever the ceiling* (500 was slower only because it
  forced a second grouping). The size bound was never exercised: the whole
  collection at precision 3 is 708 cells, under every candidate. A ceiling
  exists to bound the payload, so the criterion now measures the payload; the
  endpoint's cost at 100,000 rows is still measured and reported as the
  separate number RFC-032 section 11 asked for, and the ceiling is exercised by
  asking for precision 4, where the uncapped answer is tens of thousands of
  cells. Both versions were written by the implementation -- RFC-032 left
  `MAX_MAP_CELLS` TBM without a criterion -- which is why the correction is
  recorded here rather than made quietly.

The map phase drops the HNSW index *inside the same rolled-back transaction*
before inserting the extra 80,000 rows: the map never touches it, and
maintaining it would turn a minute of inserts into a quarter of an hour.

Everything happens inside one transaction that is rolled back, so the
development database is left exactly as it was found.

    backend/.venv/Scripts/python.exe experiments/rfc-032-geolocation/planner_check.py

`RFC032_CORPUS_SIZE`, `RFC032_MAP_CORPUS_SIZE` and `RFC032_QUERIES` override the
sizes; `RFC032_PHASES=map` runs the map phase alone, on a corpus seeded with the
HNSW index already dropped (about a minute instead of twenty).
"""

from __future__ import annotations

import datetime
import math
import os
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

from psycopg import ClientCursor  # noqa: E402
from sqlalchemy import Select, delete, text  # noqa: E402
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.sql.compiler import SQLCompiler  # noqa: E402
from sqlalchemy.sql.expression import ClauseElement, Executable  # noqa: E402

from app.application.use_cases.aggregate_positions import (  # noqa: E402
    DEFAULT_MAP_PRECISION,
    AggregatePositionsUseCase,
)
from app.domain.services.haversine import haversine_m  # noqa: E402
from app.domain.value_objects.embedding_vector import EmbeddingVector  # noqa: E402
from app.domain.value_objects.geo_circle import GeoCircle  # noqa: E402
from app.domain.value_objects.position import BoundingBox, Position  # noqa: E402
from app.domain.value_objects.search_filters import SearchFilters  # noqa: E402
from app.infrastructure.database.models.device_model import DeviceModel  # noqa: E402
from app.infrastructure.database.models.image_model import ImageModel  # noqa: E402
from app.infrastructure.database.models.indexing_job_model import (  # noqa: E402
    IndexingJobModel,
)
from app.infrastructure.persistence.engine import EngineInstance  # noqa: E402
from app.infrastructure.persistence.postgres_image_repository import (  # noqa: E402
    PostgresImageRepository,
)
from app.presentation.schemas.map_schema import MapResponseSchema  # noqa: E402

random.seed(0)

CORPUS_SIZE = int(os.environ.get("RFC032_CORPUS_SIZE", 20_000))
"""RFC-027's and RFC-028's size, so the three filters are comparable."""

MAP_CORPUS_SIZE = int(os.environ.get("RFC032_MAP_CORPUS_SIZE", 100_000))
QUERIES = int(os.environ.get("RFC032_QUERIES", 20))
LIMIT = 10
UNKNOWN_FRACTION = 0.10

SOUTH, NORTH = -26.64800, -26.22519
WEST, EAST = -49.41636, -48.81620
"""The pilot's bounding box: 59.6 km x 47.0 km (RFC-032 section 2.3)."""

SITES = 40
ZIPF_S = 0.85
SITE_SCATTER_M = 60.0
METRES_PER_DEGREE = 111_195.0

TARGETS = (0.01, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.70)
"""Fractions of the corpus inside the circle, across RFC-032's 10-50% band."""

CONFIGURATIONS: dict[str, tuple[str, ...]] = {
    "baseline": (),
    "strict_order": ("SET LOCAL hnsw.iterative_scan = strict_order",),
    "ef_search=100": ("SET LOCAL hnsw.ef_search = 100",),
    "ef_search=200": ("SET LOCAL hnsw.ef_search = 200",),
    "ef_search=400": ("SET LOCAL hnsw.ef_search = 400",),
}

MAX_CELLS_CANDIDATES = (500, 1000, 2000, 5000)
MAX_CELLS_BYTES = 256 * 1024

DEVICE_ID = uuid.UUID(int=0xD0000032)
VECTOR_LITERAL = re.compile(r"'\[[^\]]*\]'(::vector)?")


class Explain(Executable, ClauseElement):
    """`EXPLAIN ANALYZE` around a SQLAlchemy statement, binds and all."""

    inherit_cache = False

    def __init__(self, statement: Select[Any]) -> None:
        self.statement = statement


@compiles(Explain, "postgresql")
def _compile_explain(element: Explain, compiler: SQLCompiler, **kw: Any) -> str:
    return "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON) " + compiler.process(
        element.statement, **kw
    )


def seeded_uuid() -> uuid.UUID:
    """Seeded ids: the HNSW graph depends on them (RFC-028's script found out)."""
    return uuid.UUID(int=random.getrandbits(128), version=4)


def unit_vector() -> list[float]:
    values = [random.gauss(0, 1) for _ in range(512)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


@dataclass(frozen=True)
class Site:
    latitude: float
    longitude: float
    weight: float


def make_sites() -> list[Site]:
    weights = [1 / (rank**ZIPF_S) for rank in range(1, SITES + 1)]
    total = sum(weights)
    return [
        Site(
            latitude=random.uniform(SOUTH + 0.01, NORTH - 0.01),
            longitude=random.uniform(WEST + 0.01, EAST - 0.01),
            weight=weight / total,
        )
        for weight in weights
    ]


def random_position(sites: list[Site]) -> tuple[float, float] | None:
    if random.random() < UNKNOWN_FRACTION:
        return None
    site = random.choices(sites, weights=[s.weight for s in sites])[0]
    scatter = SITE_SCATTER_M / METRES_PER_DEGREE
    return (
        site.latitude + random.gauss(0, scatter),
        site.longitude
        + random.gauss(0, scatter) / math.cos(math.radians(site.latitude)),
    )


def insert_rows(
    session: Session,
    positions: list[tuple[float, float] | None],
    embedding: str | None,
) -> None:
    """Insert one row per position; a fresh random vector each unless given one."""
    written = 0
    while written < len(positions):
        chunk = positions[written : written + 500]
        rows = []
        for position in chunk:
            rows.append(
                {
                    "id": str(seeded_uuid()),
                    "device_id": str(DEVICE_ID),
                    "relative_path": f"bench/{seeded_uuid().hex}.jpg",
                    "embedding": embedding or literal(unit_vector()),
                    "latitude": position[0] if position else None,
                    "longitude": position[1] if position else None,
                    "position_source": "exif_gps" if position else "unknown",
                }
            )
        session.execute(
            text(
                "INSERT INTO images (id, device_id, relative_path, filename, "
                "extension, embedding, latitude, longitude, position_source) "
                "VALUES (:id, :device_id, :relative_path, 'bench', 'jpg', "
                ":embedding, :latitude, :longitude, :position_source)"
            ),
            rows,
        )
        written += len(chunk)


def seed_device(session: Session) -> None:
    now = datetime.datetime.now(tz=datetime.UTC)
    session.execute(
        text(
            "INSERT INTO devices (id, volume_identity, volume_kind, label, "
            "first_seen_at, last_seen_at) VALUES (:id, :identity, "
            "'windows-volume-guid', 'HD-BENCH', :now, :now)"
        ),
        {"id": str(DEVICE_ID), "identity": "\\\\?\\Volume{bench-032}\\", "now": now},
    )


def seed(session: Session, sites: list[Site]) -> list[tuple[float, float] | None]:
    seed_device(session)
    positions = [random_position(sites) for _ in range(CORPUS_SIZE)]
    insert_rows(session, positions, embedding=None)
    session.commit()
    return positions


def describe_clustering(
    positions: list[tuple[float, float] | None], sites: list[Site]
) -> None:
    placed = [p for p in positions if p is not None]
    cells = Counter((round(lat, 3), round(lon, 3)) for lat, lon in placed)
    top5_cells = sum(n for _, n in cells.most_common(5))
    top5_sites = sum(site.weight for site in sites[:5])
    print(
        f"  {len(placed)} placed, {len(positions) - len(placed)} without position; "
        f"{len(cells)} distinct ~100 m cells (5 busiest: "
        f"{100 * top5_cells / len(placed):.1f}%); the 5 busiest of {SITES} sites "
        f"hold {100 * top5_sites:.1f}% of the placed photos, the busiest alone "
        f"{100 * sites[0].weight:.1f}%"
    )


def circle_for(
    center: Position, positions: list[tuple[float, float] | None], target: float
) -> tuple[GeoCircle, int]:
    """The circle around `center` whose coverage is closest to `target`."""
    distances = sorted(
        haversine_m(center, Position(*p)) for p in positions if p is not None
    )
    wanted = max(1, round(target * len(positions)))
    radius = distances[min(wanted, len(distances)) - 1] + 0.01
    circle = GeoCircle(center, radius)
    matching = sum(1 for d in distances if d <= radius)
    return circle, matching


def compact(plan: str) -> str:
    return VECTOR_LITERAL.sub("'[512 floats]'::vector", plan)


def plan_kind(plan: str) -> str:
    """Only the HNSW branch is approximate; every other plan is exact."""
    if "ix_images_embedding_hnsw" in plan:
        return "hnsw (approximate)"
    if "ix_bench_latitude" in plan:
        return "b-tree latitude (exact)"
    if "ix_bench_position_gist" in plan:
        return "gist point (exact)"
    if "Seq Scan" in plan:
        return "seq scan (exact)"
    return "other"


def search(
    session: Session,
    query: list[float],
    filters: SearchFilters | None,
    gist: bool,
) -> Select[Any]:
    statement = PostgresImageRepository.search_statement(
        EmbeddingVector(query), LIMIT, filters
    )
    if gist and filters is not None and filters.taken_within is not None:
        box = filters.taken_within.bounding_box()
        assert box is not None
        statement = statement.where(
            text(
                "point(images.longitude, images.latitude) <@ "
                "box(point(:west, :south), point(:east, :north))"
            ).bindparams(
                west=box.min_longitude,
                south=box.min_latitude,
                east=box.max_longitude,
                north=box.max_latitude,
            )
        )
    return statement


@contextmanager
def configured(session: Session, statements: tuple[str, ...]) -> Iterator[None]:
    for statement in statements:
        session.execute(text(statement))
    try:
        yield
    finally:
        session.execute(text("SET LOCAL hnsw.iterative_scan = off"))
        session.execute(text(f"SET LOCAL hnsw.ef_search = {DEFAULT_EF_SEARCH}"))


SERVER_TIME = re.compile(r"(Planning|Execution) Time: ([0-9.]+) ms")


def server_ms(plan: str) -> float:
    """Planning plus execution time, as the server reports them."""
    return sum(float(match.group(2)) for match in SERVER_TIME.finditer(plan))


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@dataclass
class Cell:
    rows: list[int]
    server: list[float]
    wall: list[float]
    plan: str
    expected: int = LIMIT
    """`limit`, or every matching row when fewer than `limit` match at all."""

    @property
    def full(self) -> int:
        return sum(1 for rows in self.rows if rows == self.expected)

    @property
    def p95(self) -> float:
        """Server-side p95: the number the criterion reads."""
        return percentile_95(self.server)


def run_custom(
    session: Session,
    queries: list[list[float]],
    filters: SearchFilters | None,
    gist: bool,
    expected: int = LIMIT,
) -> Cell:
    """Each query planned for its own values: rows from a run, time from EXPLAIN."""
    cell = Cell(rows=[], server=[], wall=[], plan="", expected=expected)
    for query in queries:
        statement = search(session, query, filters, gist)
        started = time.perf_counter()
        cell.rows.append(len(session.execute(statement).all()))
        cell.wall.append((time.perf_counter() - started) * 1000)
        plan = "\n".join(str(row[0]) for row in session.execute(Explain(statement)))
        cell.server.append(server_ms(plan))
        cell.plan = cell.plan or plan
    return cell


def positional(statement: Select[Any]) -> tuple[str, list[Any]]:
    """The statement as `$n` SQL plus its processed values, for PREPARE/EXECUTE."""
    compiled = statement.compile(dialect=PGDialect_psycopg(paramstyle="numeric_dollar"))
    params = compiled.construct_params()
    processors = compiled._bind_processors
    assert compiled.positiontup is not None
    values = [
        processors[name](params[name]) if name in processors else params[name]
        for name in compiled.positiontup
    ]
    return str(compiled), values


def run_generic(
    session: Session,
    queries: list[list[float]],
    filters: SearchFilters | None,
    gist: bool,
    expected: int = LIMIT,
) -> Cell:
    """The same queries through one prepared statement and a forced generic plan.

    `PREPARE` / `EXECUTE` / `EXPLAIN EXECUTE` through a client-side binding
    cursor on the session's own connection, as RFC-028's script did: the
    generic plan is costed without knowing the circle, which is what a
    long-lived pooled connection may settle on for a statement whose text
    never changes.
    """
    cell = Cell(rows=[], server=[], wall=[], plan="", expected=expected)
    driver = session.connection().connection.driver_connection
    sql, _ = positional(search(session, queries[0], filters, gist))
    with ClientCursor(driver) as cursor:  # type: ignore[arg-type]
        cursor.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cursor.execute("PREPARE bench_generic AS " + sql)
        try:
            for query in queries:
                _, values = positional(search(session, query, filters, gist))
                placeholders = ", ".join(["%s"] * len(values))
                started = time.perf_counter()
                cursor.execute(f"EXECUTE bench_generic({placeholders})", values)
                cell.rows.append(len(cursor.fetchall()))
                cell.wall.append((time.perf_counter() - started) * 1000)
                cursor.execute(
                    "EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON) "
                    f"EXECUTE bench_generic({placeholders})",
                    values,
                )
                plan = "\n".join(str(row[0]) for row in cursor.fetchall())
                cell.server.append(server_ms(plan))
                cell.plan = cell.plan or plan
        finally:
            cursor.execute("DEALLOCATE bench_generic")
            cursor.execute("SET LOCAL plan_cache_mode = auto")
    return cell


def report(label: str, cell: Cell) -> None:
    short = (
        ""
        if cell.full == len(cell.rows)
        else (
            f"  <-- SHORT: {len(cell.rows) - cell.full} of {len(cell.rows)} queries, "
            f"min {min(cell.rows)} rows"
        )
    )
    print(
        f"  {label:<34} full {cell.full:2d}/{len(cell.rows)}  server p50 "
        f"{statistics.median(cell.server):6.1f} p95 {cell.p95:6.1f} ms  "
        f"(wall p95 {percentile_95(cell.wall):6.1f})  {plan_kind(cell.plan)}{short}"
    )


@dataclass
class Verdict:
    passes: bool
    worst_p95: float


def measure_setup(
    session: Session,
    heading: str,
    queries: list[list[float]],
    circles: list[tuple[float, GeoCircle, int]],
    unfiltered_p95: float,
    gist: bool,
) -> dict[str, Verdict]:
    print(f"===== {heading} =====")
    verdicts: dict[str, Verdict] = {}
    for name, statements in CONFIGURATIONS.items():
        print(f"--- {name} ---")
        passes = True
        worst = 0.0
        for kind in ("custom", "generic"):
            for target, circle, matching in circles:
                filters = SearchFilters(taken_within=circle)
                label = (
                    f"{kind:<7} {matching / CORPUS_SIZE:5.1%} "
                    f"(r={circle.radius_m:,.0f} m)"
                )
                with configured(session, statements):
                    expected = min(LIMIT, matching)
                    if kind == "custom":
                        cell = run_custom(session, queries, filters, gist, expected)
                    else:
                        cell = run_generic(session, queries, filters, gist, expected)
                report(label, cell)
                if PRINT_PLANS and cell.plan:
                    print("      " + compact(cell.plan).replace("\n", "\n      "))
                passes = passes and cell.full == len(cell.rows)
                worst = max(worst, cell.p95)
        within = worst <= 2 * unfiltered_p95
        verdicts[name] = Verdict(passes=passes and within, worst_p95=worst)
        print(
            f"  => {name}: every query full: {'yes' if passes else 'NO'}; worst "
            f"filtered server p95 {worst:.1f} ms vs 2x unfiltered "
            f"{2 * unfiltered_p95:.1f} ms: {'within' if within else 'OVER'}; "
            f"{'PASSES' if verdicts[name].passes else 'fails'}"
        )
        print()
    return verdicts


def vacuum_images() -> None:
    """Clear dead tuples from earlier runs (RFC-028's script found they skew plans)."""
    with EngineInstance.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as autocommit:
        autocommit.execute(text("VACUUM (ANALYZE) images"))


def index_effect(session: Session, whole: BoundingBox, ten_km: BoundingBox) -> None:
    """Does a B-tree on `latitude` speed up the map? Asked so order cannot answer.

    Added after the second run, which showed the whole-collection grouping
    falling from ~867 ms to ~347 ms "with the index" -- while the plan was a
    sequential scan in both setups, so the index could not have been the
    cause. The section measured first runs right after a bulk insert and
    pays for setting hint bits on 100,000 fresh tuples. Here the table is
    warmed with three full scans first, and the two setups alternate, four
    rounds each, so neither is always first. Server time only.
    """
    for _ in range(3):
        session.execute(text("SELECT count(*), sum(latitude) FROM images")).all()
    timings: dict[tuple[str, str], list[float]] = {}
    scans: dict[tuple[str, str], set[str]] = {}
    for _ in range(4):
        for setup in ("no index", "b-tree on latitude"):
            if setup != "no index":
                session.execute(
                    text("CREATE INDEX ix_bench_latitude ON images (latitude)")
                )
                session.execute(text("ANALYZE images"))
            for label, area in (
                ("whole collection", whole),
                ("10 km viewport", ten_km),
            ):
                statement = PostgresImageRepository.aggregate_statement(
                    SearchFilters(), area, DEFAULT_MAP_PRECISION, None
                )
                plan = "\n".join(str(r[0]) for r in session.execute(Explain(statement)))
                timings.setdefault((setup, label), []).append(server_ms(plan))
                scans.setdefault((setup, label), set()).add(
                    "index" if "ix_bench_latitude" in plan else "seq scan"
                )
            if setup != "no index":
                session.execute(text("DROP INDEX ix_bench_latitude"))
    print(
        "--- index effect at the default precision: warmed, alternating, "
        "4 rounds, server ms ---"
    )
    for label in ("whole collection", "10 km viewport"):
        without = statistics.median(timings[("no index", label)])
        with_index = statistics.median(timings[("b-tree on latitude", label)])
        print(
            f"  {label:<17} no index {without:7.1f} ms "
            f"({'/'.join(sorted(scans[('no index', label)]))}); b-tree "
            f"{with_index:7.1f} ms ({'/'.join(sorted(scans[('b-tree on latitude', label)]))})"
            f"; ratio {without / with_index:4.2f}x"
        )
    print()


def map_phase(
    session: Session, sites: list[Site], positions: list[tuple[float, float] | None]
) -> None:
    print("===== map: GET /images/map at %d rows =====" % MAP_CORPUS_SIZE)
    session.execute(text("DROP INDEX IF EXISTS ix_bench_latitude"))
    session.execute(text("DROP INDEX IF EXISTS ix_bench_position_gist"))
    session.execute(text("DROP INDEX IF EXISTS ix_images_embedding_hnsw"))
    extra = [random_position(sites) for _ in range(MAP_CORPUS_SIZE - len(positions))]
    started = time.perf_counter()
    insert_rows(session, extra, embedding=literal(unit_vector()))
    session.execute(text("ANALYZE images"))
    print(f"  {len(extra)} rows added in {time.perf_counter() - started:.0f} s")
    describe_clustering(positions + extra, sites)
    repository = PostgresImageRepository(session)
    whole = BoundingBox(SOUTH, WEST, NORTH, EAST)
    busiest = sites[0]
    ten_km = BoundingBox(
        busiest.latitude - 0.045,
        busiest.longitude - 0.05,
        busiest.latitude + 0.045,
        busiest.longitude + 0.05,
    )

    index_effect(session, whole, ten_km)

    def timed(area: BoundingBox, precision: int) -> tuple[int, float, float, str]:
        timings = []
        cells = []
        for _ in range(5):
            started = time.perf_counter()
            cells = repository.aggregate_positions(
                SearchFilters(), area, precision, None
            )
            timings.append((time.perf_counter() - started) * 1000)
        statement = PostgresImageRepository.aggregate_statement(
            SearchFilters(), area, precision, None
        )
        plan = "\n".join(str(r[0]) for r in session.execute(Explain(statement)))
        return len(cells), statistics.median(timings), server_ms(plan), plan

    for heading, ddl in (
        ("no spatial index", None),
        ("b-tree on latitude", "CREATE INDEX ix_bench_latitude ON images (latitude)"),
    ):
        if ddl:
            session.execute(text(ddl))
            session.execute(text("ANALYZE images"))
        print(f"--- {heading} ---")
        for label, area in (("whole collection", whole), ("10 km viewport", ten_km)):
            for precision in range(0, 6):
                count, median, server, plan = timed(area, precision)
                scan = (
                    "index"
                    if "ix_bench_latitude" in plan
                    else ("seq scan" if "Seq Scan" in plan else "other")
                )
                print(
                    f"  {label:<17} precision {precision}: {count:6d} cells, "
                    f"wall median {median:7.1f} ms, server {server:7.1f} ms, {scan}"
                )
        started = time.perf_counter()
        unknown = repository.count_unknown_position(SearchFilters())
        print(
            f"  count_unknown_position: {unknown} in "
            f"{(time.perf_counter() - started) * 1000:.1f} ms"
        )
        print()

    print("--- the whole map answer at the default precision (endpoint cost) ---")
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        answer = AggregatePositionsUseCase(repository, max_cells=10**9).execute(
            whole, SearchFilters(), DEFAULT_MAP_PRECISION
        )
        timings.append((time.perf_counter() - started) * 1000)
    print(
        f"  precision {DEFAULT_MAP_PRECISION}: {len(answer.cells)} cells, "
        f"use case wall median {statistics.median(timings):6.1f} ms "
        "(grouping plus the unknown-position count)"
    )
    print()

    print("--- MAX_MAP_CELLS ---")
    uncapped = AggregatePositionsUseCase(repository, max_cells=10**9).execute(
        whole, SearchFilters(), 5
    )
    body = MapResponseSchema.from_map(uncapped).model_dump_json()
    per_cell = len(body) / len(uncapped.cells)
    print(
        "  bytes per cell, measured on the MapResponseSchema JSON of the uncapped "
        f"precision-5 answer: {len(body)} bytes / {len(uncapped.cells)} cells = "
        f"{per_cell:.1f}"
    )
    chosen = None
    for candidate in MAX_CELLS_CANDIDATES:
        use_case = AggregatePositionsUseCase(repository, max_cells=candidate)
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            answer = use_case.execute(whole, SearchFilters(), 4)
            timings.append((time.perf_counter() - started) * 1000)
        actual = len(MapResponseSchema.from_map(answer).model_dump_json())
        worst = candidate * per_cell
        fits = worst <= MAX_CELLS_BYTES
        if fits:
            chosen = candidate
        print(
            f"  max_cells {candidate:5d}: worst case {worst / 1024:6.1f} KB "
            f"({'fits' if fits else 'over'}); whole collection asked at precision "
            f"4 -> {answer.precision_applied}, {len(answer.cells)} cells, "
            f"{actual / 1024:5.1f} KB, wall median {statistics.median(timings):6.1f} ms"
        )
    print(
        f"  => MAX_MAP_CELLS = {chosen} (largest whose worst case is within "
        f"{MAX_CELLS_BYTES // 1024} KB)"
    )
    print()


PRINT_PLANS = os.environ.get("RFC032_PRINT_PLANS", "1") == "1"
PHASES = set(os.environ.get("RFC032_PHASES", "search,map").split(","))
DEFAULT_EF_SEARCH = 40


def main() -> None:
    global DEFAULT_EF_SEARCH
    vacuum_images()
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    driver = connection.connection.driver_connection
    driver.prepare_threshold = None  # type: ignore[union-attr]

    try:
        session.execute(delete(ImageModel))
        session.execute(delete(IndexingJobModel))
        session.execute(delete(DeviceModel))
        session.commit()

        sites = make_sites()
        if "search" not in PHASES:
            print("map phase only (RFC032_PHASES=map)")
            seed_device(session)
            map_phase(session, sites, [])
            return
        print(
            f"seeding {CORPUS_SIZE} images: {SITES} sites, Zipf s={ZIPF_S}, "
            f"{SITE_SCATTER_M:.0f} m scatter, {UNKNOWN_FRACTION:.0%} without position"
        )
        started = time.perf_counter()
        positions = seed(session, sites)
        session.execute(text("ANALYZE images"))
        print(f"  seeded in {time.perf_counter() - started:.0f} s")
        describe_clustering(positions, sites)
        DEFAULT_EF_SEARCH = int(
            session.execute(text("SHOW hnsw.ef_search")).scalar_one()
        )
        version = session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        server = session.execute(text("SHOW server_version")).scalar_one()
        print(
            f"PostgreSQL {server}, pgvector {version}, hnsw.ef_search = "
            f"{DEFAULT_EF_SEARCH}, limit = {LIMIT}, {QUERIES} query vectors per cell"
        )
        print()

        queries = [unit_vector() for _ in range(QUERIES)]
        center = Position(sites[0].latitude, sites[0].longitude)
        circles = []
        for target in TARGETS:
            circle, matching = circle_for(center, positions, target)
            circles.append((target, circle, matching))

        print("===== unfiltered search (the RFC-025 query, never mitigated) =====")
        unfiltered = run_custom(session, queries, None, False)
        report("unfiltered", unfiltered)
        if PRINT_PLANS:
            print("      " + compact(unfiltered.plan).replace("\n", "\n      "))
        print()

        results: dict[str, dict[str, Verdict]] = {}
        results["no spatial index"] = measure_setup(
            session,
            "schema as migrated: no spatial index",
            queries,
            circles,
            unfiltered.p95,
            gist=False,
        )
        session.execute(text("CREATE INDEX ix_bench_latitude ON images (latitude)"))
        session.execute(text("ANALYZE images"))
        results["b-tree latitude"] = measure_setup(
            session,
            "b-tree on latitude (rolled back)",
            queries,
            circles,
            unfiltered.p95,
            gist=False,
        )
        session.execute(text("DROP INDEX ix_bench_latitude"))
        session.execute(
            text(
                "CREATE INDEX ix_bench_position_gist ON images "
                "USING gist (point(longitude, latitude))"
            )
        )
        session.execute(text("ANALYZE images"))
        results["gist point"] = measure_setup(
            session,
            "gist on point(longitude, latitude) + <@ box (rolled back)",
            queries,
            circles,
            unfiltered.p95,
            gist=True,
        )
        session.execute(text("DROP INDEX ix_bench_position_gist"))

        print("===== verdict (criteria in the module docstring) =====")
        print(
            f"  unfiltered server p95: {unfiltered.p95:.1f} ms; ceiling 2x = "
            f"{2 * unfiltered.p95:.1f} ms"
        )
        for setup, verdicts in results.items():
            passing = {n: v for n, v in verdicts.items() if v.passes}
            best = min(passing, key=lambda n: passing[n].worst_p95) if passing else None
            print(
                f"  {setup:<17}: passing = {sorted(passing) or 'none'}; cheapest = "
                f"{best} ({passing[best].worst_p95:.1f} ms)"
                if best
                else f"  {setup:<17}: passing = none"
            )
        print()

        map_phase(session, sites, positions)
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        print("rolled back; the development database is unchanged")


if __name__ == "__main__":
    main()
