"""Unit tests for `app.cli.search` -- the CLI's composition of search.

`format_hits()` is tested as a pure function; `run()` is tested the way
`test_indexing_worker.py::test_main_creates_a_job_and_runs_it_here` tests
`indexing_worker.main()` -- by monkeypatching the concrete classes `run()`
imports (function-local, so patching their origin module works) with
fakes, so no Postgres connection and no CLIP checkpoint are ever touched.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from tests.application.fakes import FakeImageRepository
from tests.conftest import TEST_DEVICE_ID

from app.cli import search
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.search_hit import SearchHit
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel


def _image(name: str) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/{name}.png"),
        filename=name,
        extension="png",
    )


class TestFormatHits:
    def test_no_hits_says_so_explicitly(self) -> None:
        assert search.format_hits([]) == "No results."

    def test_hits_are_numbered_most_similar_first(self) -> None:
        first = SearchHit(image=_image("lake"), similarity=0.812345)
        second = SearchHit(image=_image("barn"), similarity=0.5)

        rendered = search.format_hits([first, second])

        lines = rendered.splitlines()
        assert lines[0].startswith("1. 0.812")
        assert "images/lake.png" in lines[0]
        assert str(TEST_DEVICE_ID) in lines[0]
        assert lines[1].startswith("2. 0.500")
        assert "images/barn.png" in lines[1]


class TestRun:
    def test_composes_the_use_case_and_prints_the_results(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        image = _image("seeded-0")
        repository = FakeImageRepository()
        repository.seed_embedding(image, EmbeddingVector([1.0] + [0.0] * 511))
        closed: list[bool] = []

        class _Session:
            def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", lambda: _Session()
        )
        monkeypatch.setattr(
            "app.infrastructure.persistence.postgres_image_repository."
            "PostgresImageRepository",
            lambda session: repository,
        )
        monkeypatch.setattr(
            "app.presentation.dependencies.get_embedding_model",
            lambda: FakeEmbeddingModel(),
        )

        search.run("a house near water", limit=5)

        captured = capsys.readouterr()
        assert "images/seeded-0.png" in captured.out
        (call,) = repository.search_similar_calls
        assert call[1] == 5
        assert closed == [True]

    def test_the_session_is_closed_even_when_the_search_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []

        class _Session:
            def close(self) -> None:
                closed.append(True)

        class _ExplodingRepository(FakeImageRepository):
            def search_similar(
                self,
                embedding: EmbeddingVector,
                limit: int,
                filters: object = None,
            ) -> list[SearchHit]:
                raise RuntimeError("boom")

        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", lambda: _Session()
        )
        monkeypatch.setattr(
            "app.infrastructure.persistence.postgres_image_repository."
            "PostgresImageRepository",
            lambda session: _ExplodingRepository(),
        )
        monkeypatch.setattr(
            "app.presentation.dependencies.get_embedding_model",
            lambda: FakeEmbeddingModel(),
        )

        with pytest.raises(RuntimeError):
            search.run("anything", limit=None)

        assert closed == [True]


class TestQueryImage:
    def test_it_wraps_the_file_where_the_adapter_reads_it(self, tmp_path: Path) -> None:
        picture = tmp_path / "Print.JPG"

        image = search.query_image(picture)

        assert image.absolute_path == ImagePath(picture.resolve())
        assert image.filename == "Print"
        assert image.extension == "jpg"

    def test_it_belongs_to_no_device(self, tmp_path: Path) -> None:
        """Never persisted: the nil UUID says so rather than borrowing a real disk."""
        assert search.query_image(tmp_path / "a.jpg").device_id.value == uuid.UUID(
            int=0
        )

    def test_the_same_file_builds_the_same_query(self, tmp_path: Path) -> None:
        assert (
            search.query_image(tmp_path / "a.jpg").id
            == search.query_image(tmp_path / "a.jpg").id
        )


class TestRunImage:
    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        repository: FakeImageRepository,
        closed: list[bool],
    ) -> FakeEmbeddingModel:
        model = FakeEmbeddingModel()

        class _Session:
            def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", lambda: _Session()
        )
        monkeypatch.setattr(
            "app.infrastructure.persistence.postgres_image_repository."
            "PostgresImageRepository",
            lambda session: repository,
        )
        monkeypatch.setattr(
            "app.presentation.dependencies.get_embedding_model", lambda: model
        )
        return model

    def test_ranks_the_index_against_the_pictures_own_embedding(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        picture = tmp_path / "print.jpg"
        picture.write_bytes(b"pixels")
        repository = FakeImageRepository()
        repository.seed_embedding(
            _image("original"), EmbeddingVector([1.0] + [0.0] * 511)
        )
        closed: list[bool] = []
        model = self._wire(monkeypatch, repository, closed)

        search.run_image(picture, limit=20)

        assert "images/original.png" in capsys.readouterr().out
        ((embedding, limit, _),) = repository.search_similar_calls
        assert embedding == model.encode_image(search.query_image(picture))
        assert limit == 20
        assert closed == [True]

    def test_a_missing_file_is_refused_before_anything_is_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse() -> None:
            raise AssertionError("the model must not be built for a missing file")

        monkeypatch.setattr("app.presentation.dependencies.get_embedding_model", refuse)
        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", refuse
        )

        with pytest.raises(SystemExit, match="No image file"):
            search.run_image(tmp_path / "nowhere.jpg", limit=None)

    def test_the_session_is_closed_even_when_the_search_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        picture = tmp_path / "print.jpg"
        picture.write_bytes(b"pixels")

        class _ExplodingRepository(FakeImageRepository):
            def search_similar(
                self,
                embedding: EmbeddingVector,
                limit: int,
                filters: object = None,
            ) -> list[SearchHit]:
                raise RuntimeError("boom")

        closed: list[bool] = []
        self._wire(monkeypatch, _ExplodingRepository(), closed)

        with pytest.raises(RuntimeError):
            search.run_image(picture, limit=None)

        assert closed == [True]
