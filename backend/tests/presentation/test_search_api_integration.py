"""Level 2 of RFC-026's three: the real graph, minus the checkpoint.

`TestClient` over the production dependency wiring, a real PostgreSQL
session, real pgvector, and `FakeEmbeddingModel` in place of CLIP. That
substitution is what makes this level worth having separately from level
3: everything between the query string and the JSON body -- dependency
injection, session lifetime, the repository, the vector index, and
serialization -- is exercised without paying ~5 s for a checkpoint.

The fake has no semantics, which is fine, because semantics is level 3's
job. It does have usable *geometry*: deterministic across processes
(SHA-256 in counter mode rather than a salted `hash()`) and mean-centred
unit vectors, fixed in RFC-022 section 7.4 precisely so that similarity
search over it is not trivially degenerate. Every expectation below is
therefore arithmetic rather than approximate: an image stored with the
query's own vector scores exactly 1, its negation exactly -1, and an
unrelated seed lands somewhere between.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.persistence.engine import EngineInstance
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import get_db
from app.presentation.api import app
from app.presentation.dependencies import get_embedding_model

SEARCH_URL = "/api/v1/images/search"
QUERY = "fish ponds"


def checked_out_connections() -> int:
    """How many pooled connections are currently lent out.

    The `isinstance` guard is not ceremony: `checkedout()` exists on
    `QueuePool` and not on `Pool`, and an engine built with `NullPool`
    would make the count below meaningless rather than wrong -- every
    number would be zero and the leak test would pass by construction.
    """
    pool = EngineInstance.pool
    assert isinstance(pool, QueuePool), f"unexpected pool {type(pool).__name__}"
    return pool.checkedout()


def make_image(filename: str) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath(f"images/search/{filename}-{uuid.uuid4().hex}.jpg"),
        filename=filename,
        extension="jpg",
    )


def store(
    repository: PostgresImageRepository,
    filename: str,
    embedding: EmbeddingVector | None,
) -> Image:
    """Persist one image through the same door the indexing worker uses."""
    image = make_image(filename)
    if embedding is None:
        repository.save(image)
        return image
    repository.save_indexed(
        IndexingRecord(
            image=image,
            embedding=embedding,
            file_size=1,
            file_modified_at=None,
        )
    )
    return image


@pytest.fixture
def model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel()


@pytest.fixture
def seeded(empty_db_session: Session, model: FakeEmbeddingModel) -> dict[str, Image]:
    """Four rows whose ranking against `QUERY` is known exactly.

    `empty_db_session` matters as much as the rows themselves: search is a
    top-K query over the whole table, so a row left behind by unrelated
    work can displace an expected result. It empties the table inside the
    transaction it later rolls back, leaving the development database
    untouched.
    """
    repository = PostgresImageRepository(empty_db_session)
    query_vector = model.encode_text(QUERY)

    return {
        "identical": store(repository, "identical", query_vector),
        "unrelated": store(repository, "unrelated", model.encode_text("a cat")),
        "opposite": store(
            repository,
            "opposite",
            EmbeddingVector([-value for value in query_vector.values]),
        ),
        "unindexed": store(repository, "unindexed", None),
    }


@pytest.fixture
def client(
    empty_db_session: Session, model: FakeEmbeddingModel
) -> Iterator[TestClient]:
    """The real app, with the session and the model swapped at their seams.

    Both overrides exist for isolation rather than convenience: the
    session so that everything a request writes or reads happens inside
    the fixture's transaction, and the model so that no checkpoint is
    downloaded. Everything between them -- the router, the composition
    root, the use case, the repository -- is the production object.
    """
    app.dependency_overrides[get_db] = lambda: empty_db_session
    app.dependency_overrides[get_embedding_model] = lambda: model
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_a_request_over_seeded_rows_comes_back_ranked(
    client: TestClient, seeded: dict[str, Image]
) -> None:
    response = client.get(SEARCH_URL, params={"q": QUERY})

    assert response.status_code == 200
    body = response.json()
    assert [result["filename"] for result in body["results"]] == [
        "identical",
        "unrelated",
        "opposite",
    ]
    assert body["results"][0]["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert body["results"][-1]["similarity"] == pytest.approx(-1.0, abs=1e-6)


def test_a_row_without_an_embedding_never_appears(
    client: TestClient, seeded: dict[str, Image]
) -> None:
    """RFC-025 section 6: an image that was seen but not encoded is not a match.

    `IndexImageUseCase` can store a row with a NULL embedding, and the
    repository filters those out in SQL. A regression there would surface
    here as a fourth result with a meaningless score.
    """
    response = client.get(SEARCH_URL, params={"q": QUERY})

    returned = {result["id"] for result in response.json()["results"]}
    assert str(seeded["unindexed"].id.value) not in returned
    assert len(returned) == 3


def test_limit_truncates_the_real_result_set(
    client: TestClient, seeded: dict[str, Image]
) -> None:
    response = client.get(SEARCH_URL, params={"q": QUERY, "limit": 2})

    body = response.json()
    assert body["limit"] == 2
    assert [result["filename"] for result in body["results"]] == [
        "identical",
        "unrelated",
    ]


def test_the_ids_in_the_response_are_the_ids_in_the_database(
    client: TestClient, seeded: dict[str, Image]
) -> None:
    """The id is the only identifier the response publishes, so it must be real.

    RFC-027's `GET /images/{id}` will be built on exactly these values;
    a route that serialized a row number, an index position, or a
    stringified `ImageId` wrapper would look plausible in the body and be
    useless to the next endpoint.
    """
    response = client.get(SEARCH_URL, params={"q": QUERY})

    returned = {result["id"] for result in response.json()["results"]}
    assert returned == {
        str(seeded[name].id.value) for name in ("identical", "unrelated", "opposite")
    }


def test_every_request_returns_its_connection_to_the_pool(
    model: FakeEmbeddingModel,
) -> None:
    """The bug RFC-026 section 7 exists to fix, and the only test that sees it.

    Deliberately does *not* override `get_db`: the point is the real
    provider's `finally: session.close()`, and a fixture-supplied session
    would hide it. Before RFC-026 the provider called `SessionLocal()`
    and nothing ever closed the result, so each request parked a
    connection in an open transaction until the garbage collector
    happened to reclaim it -- a leak that a test asserting only on a 200
    would sail straight past, and that shows up in production as
    intermittent pool timeouts and blocked autovacuum.

    Reads only, against whatever the development database happens to
    hold, because what is being measured is the connection count rather
    than the ranking.
    """
    app.dependency_overrides[get_embedding_model] = lambda: model
    try:
        with TestClient(app) as test_client:
            baseline = checked_out_connections()

            for _ in range(5):
                response = test_client.get(SEARCH_URL, params={"q": QUERY})
                assert response.status_code == 200

            assert checked_out_connections() == baseline
    finally:
        app.dependency_overrides.clear()
