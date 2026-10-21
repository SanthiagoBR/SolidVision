"""A `limit` above 40 under the HNSW index (RFC-038 sections 2.2 and 4.3).

The README of pgvector: *"Results are limited by the size of the dynamic
candidate list (`hnsw.ef_search`), which is 40 by default."* Under the index, a
`limit=100` with no filter came back with 40 and no error, and the interface's
*"Mostrar mais"* would show 40 and disappear.

**Which plan answers depends on the server's major version**, which the
implementation of RFC-038 found and RFC-032 could not have: it measured on the
PostgreSQL 17 of Docker. `search_statement()` orders by `distance, id`. An
HNSW index provides the first key only, and serving the second takes an
incremental sort over an ordering-operator scan -- which PostgreSQL 17 does
and 16 does not. On the embedded 16 the planner answers every search with a
sequential scan: exact, and the 40 never happens there. On 17 it uses the
index once the table is large enough, and the 40 does.

So the tests hold the two things that are true everywhere: what the search
sends (nothing up to 40, one `set_config` above), and what that does to an
HNSW scan on this very server -- observed through a statement ordered by
distance alone, which both versions serve from the index.

**The vectors are a small ball of random points around a random direction**,
and both halves of that are load-bearing:

- *A ball, not a star.* The first version pushed one base vector a little
  along a different axis for each row, and the scan came back with 33 rows
  whatever `ef_search` was -- in a fresh index too. HNSW keeps a neighbour only
  if it is closer to the new element than to the neighbours already kept, so
  in a star every point links to the centre alone, and the centre's list holds
  32: 33 reachable. Random points in a ball are the well-conditioned case.
- *A random direction per test.* The suite inserts and rolls back thousands of
  rows into this one index, and an HNSW graph keeps aborted entries until a
  vacuum. In 512 dimensions two random directions are nearly orthogonal, so
  another run's debris is as far from this run's query as anything else.

The ball's radius is about 0.23 against a distance near 1 to anything random,
so the 150 of the test are the query's nearest neighbours by a wide margin.
"""

from __future__ import annotations

import math
import random
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Select, event, select, text
from sqlalchemy.orm import Session
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.database.models.image_model import (
    EMBEDDING_DIMENSION,
    ImageModel,
)
from app.infrastructure.persistence.postgres_image_repository import (
    PGVECTOR_DEFAULT_EF_SEARCH,
    PostgresImageRepository,
)

ROWS = 150
INDEX = "ix_images_embedding_hnsw"
BALL = 0.01
"""Per coordinate: a radius of about 0.01 x sqrt(512), or 0.23."""


@pytest.fixture()
def generator() -> random.Random:
    return random.Random(uuid.uuid4().int)


@pytest.fixture()
def query(generator: random.Random) -> EmbeddingVector:
    """A unit vector in a direction no earlier run used."""
    values = [generator.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSION)]
    norm = math.sqrt(sum(value * value for value in values))
    return EmbeddingVector(value / norm for value in values)


def near(base: EmbeddingVector, generator: random.Random) -> EmbeddingVector:
    """A random point in a small ball around `base`."""
    return EmbeddingVector(
        value + BALL * generator.gauss(0.0, 1.0) for value in base.values
    )


@pytest.fixture()
def indexed(
    empty_db_session: Session, query: EmbeddingVector, generator: random.Random
) -> PostgresImageRepository:
    """150 rows around the query, and the planner told to use an index."""
    repository = PostgresImageRepository(empty_db_session)
    records = []
    for number in range(ROWS):
        name = f"ef_{number:03d}"
        records.append(
            IndexingRecord(
                image=Image(
                    id=ImageId(uuid.uuid4()),
                    device_id=TEST_DEVICE_ID,
                    relative_path=ImagePath(f"ef/{name}.jpg"),
                    filename=name,
                    extension="jpg",
                ),
                embedding=near(query, generator),
                file_size=1,
                file_modified_at=None,
                content_hash=None,
            )
        )
    repository.save_indexed_many(records)
    empty_db_session.execute(text("SET LOCAL enable_seqscan = off"))
    return repository


@pytest.fixture()
def statements(empty_db_session: Session) -> Iterator[list[tuple[str, Any]]]:
    """Every statement the session sends, with its parameters."""
    seen: list[tuple[str, Any]] = []
    connection = empty_db_session.connection()

    def record(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        seen.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(connection, "before_cursor_execute", record)


def by_distance_alone(query: EmbeddingVector, limit: int) -> Select[Any]:
    """The search's ranking without its tie-break: what any HNSW scan serves."""
    distance = ImageModel.embedding.cosine_distance(list(query.values))
    return (
        select(ImageModel.id)
        .where(ImageModel.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    )


def plan_of(session: Session, statement: Select[Any]) -> str:
    compiled = statement.compile(
        dialect=session.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )
    rows = session.execute(text(f"EXPLAIN {compiled}")).scalars().all()
    return "\n".join(str(row) for row in rows)


def server_major(session: Session) -> int:
    number = int(session.execute(text("SHOW server_version_num")).scalar_one())
    return number // 10_000


def test_the_index_returns_the_candidate_list_the_search_set(
    indexed: PostgresImageRepository,
    empty_db_session: Session,
    query: EmbeddingVector,
) -> None:
    """The mechanism, on the server the suite runs against: 40, then 100."""
    statement = by_distance_alone(query, 100)
    assert INDEX in plan_of(empty_db_session, statement), "not an index scan"
    assert len(empty_db_session.execute(statement).all()) == 40

    indexed.search_similar(query, 100)

    assert len(empty_db_session.execute(statement).all()) == 100


def test_a_limit_of_100_without_a_filter_returns_100(
    indexed: PostgresImageRepository, query: EmbeddingVector
) -> None:
    """Under the index on PostgreSQL 17, by a sequential scan on 16."""
    hits = indexed.search_similar(query, 100)

    assert len(hits) == 100
    similarities = [hit.similarity for hit in hits]
    assert similarities == sorted(similarities, reverse=True)


def test_which_plan_answers_the_search_depends_on_the_server(
    indexed: PostgresImageRepository,
    empty_db_session: Session,
    query: EmbeddingVector,
) -> None:
    """The tie-break `id` needs an incremental sort over the index (PostgreSQL 17).

    A tripwire rather than a wish: the day the embedded runtime moves to 17,
    the search becomes approximate under the index -- the regime RFC-028
    section 8.1 measured -- and this says so.
    """
    statement = PostgresImageRepository.search_statement(query, 100)
    plan = plan_of(empty_db_session, statement)

    if server_major(empty_db_session) >= 17:
        assert INDEX in plan
    else:
        assert INDEX not in plan
        assert "Seq Scan on images" in plan


def test_up_to_40_the_query_is_the_one_already_measured(
    indexed: PostgresImageRepository,
    statements: list[tuple[str, Any]],
    query: EmbeddingVector,
) -> None:
    """RFC-032 section 6.1, rule 4: no mitigation may change the validated path."""
    hits = indexed.search_similar(query, PGVECTOR_DEFAULT_EF_SEARCH)

    assert len(hits) == PGVECTOR_DEFAULT_EF_SEARCH
    assert statements, "the listener saw nothing: the check would be vacuous"
    assert not any("set_config" in statement for statement, _ in statements)


def test_above_40_the_candidate_list_is_the_limit_for_this_transaction_only(
    indexed: PostgresImageRepository,
    statements: list[tuple[str, Any]],
    empty_db_session: Session,
    query: EmbeddingVector,
) -> None:
    indexed.search_similar(query, 41)

    configured = [
        parameters for statement, parameters in statements if "set_config" in statement
    ]
    assert len(configured) == 1
    values = (
        list(configured[0].values())
        if isinstance(configured[0], dict)
        else list(configured[0])
    )
    assert values == ["hnsw.ef_search", "41", True]
    assert empty_db_session.execute(text("SHOW hnsw.ef_search")).scalar_one() == "41"
