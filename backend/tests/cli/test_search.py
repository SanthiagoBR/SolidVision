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
from app.domain.value_objects.geo_circle import GeoCircle
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position import Position
from app.domain.value_objects.position_source import PositionSource
from app.domain.value_objects.search_filters import SearchFilters
from app.domain.value_objects.search_hit import SearchHit
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.config.settings import settings

# A point near Joinville, and one ~2.2 km north of it.
HERE = Position(-26.3214, -48.8163)
ELSEWHERE = Position(-26.3014, -48.8163)


def _image(
    name: str,
    position: Position | None = None,
    source: PositionSource | None = None,
) -> Image:
    if position is not None and source is None:
        source = PositionSource.EXIF_GPS
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(f"images/{name}.png"),
        filename=name,
        extension="png",
        latitude=position.latitude if position is not None else None,
        longitude=position.longitude if position is not None else None,
        position_source=source,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    repository: FakeImageRepository,
    closed: list[bool],
) -> FakeEmbeddingModel:
    """Replace the session, repository and model `_search()` composes."""
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


def _seeded(*images: Image) -> FakeImageRepository:
    repository = FakeImageRepository()
    for image in images:
        repository.seed_embedding(image, EmbeddingVector([1.0] + [0.0] * 511))
    return repository


class TestFormatPosition:
    def test_a_known_position_is_lat_lon_to_six_places(self) -> None:
        """The order and form a map's search box accepts pasted as is."""
        image = _image("lake", Position(-26.3214, -48.81630049))

        assert search.format_position(image) == "-26.321400, -48.816300"

    def test_an_examined_file_without_gps_has_no_position(self) -> None:
        image = _image("lake", source=PositionSource.UNKNOWN)

        assert search.format_position(image) == "no position"

    def test_a_never_examined_file_is_worded_apart(self) -> None:
        """Indexed before RFC-032: `exif_backfill` can still read it."""
        assert search.format_position(_image("lake")) == "position not read yet"


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

    def test_every_hit_carries_where_it_was_taken(self) -> None:
        located = SearchHit(image=_image("lake", HERE), similarity=0.8)
        unlocated = SearchHit(
            image=_image("barn", source=PositionSource.UNKNOWN), similarity=0.5
        )

        lines = search.format_hits([located, unlocated]).splitlines()

        assert lines[0].endswith("-26.321400, -48.816300")
        assert lines[1].endswith("no position")


class TestFormatHiddenByUnknownPosition:
    def test_one_image_is_singular(self) -> None:
        assert search.format_hidden_by_unknown_position(1).startswith(
            "1 indexed image has no known position"
        )

    def test_several_images_are_plural(self) -> None:
        assert search.format_hidden_by_unknown_position(12).startswith(
            "12 indexed images have no known position"
        )


class TestSearchFilters:
    def test_no_circle_is_no_filter_at_all(self) -> None:
        """`None`, not an empty `SearchFilters`: the search RFC-025 shipped."""
        assert search.search_filters(None, None) is None

    def test_a_centre_and_a_radius_make_a_circle(self) -> None:
        filters = search.search_filters([-26.3214, -48.8163], 300.0)

        assert filters == SearchFilters(
            taken_within=GeoCircle(center=HERE, radius_m=300.0)
        )

    @pytest.mark.parametrize(
        ("near", "radius_m"), [([-26.3214, -48.8163], None), (None, 300.0)]
    )
    def test_half_a_circle_is_refused_rather_than_completed(
        self, near: list[float] | None, radius_m: float | None
    ) -> None:
        with pytest.raises(SystemExit, match="go together"):
            search.search_filters(near, radius_m)

    def test_a_centre_off_the_planet_is_refused_with_the_domains_message(
        self,
    ) -> None:
        with pytest.raises(SystemExit, match="latitude 95"):
            search.search_filters([95.0, -48.8163], 300.0)

    def test_a_zero_radius_is_refused_with_the_domains_message(self) -> None:
        with pytest.raises(SystemExit, match="positive number of metres"):
            search.search_filters([-26.3214, -48.8163], 0.0)


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
        model = _wire(monkeypatch, repository, closed)

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
        _wire(monkeypatch, _ExplodingRepository(), closed)

        with pytest.raises(RuntimeError):
            search.run_image(picture, limit=None)

        assert closed == [True]

    def test_the_circle_narrows_a_picture_search_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        picture = tmp_path / "print.jpg"
        picture.write_bytes(b"pixels")
        repository = _seeded(_image("here", HERE), _image("far", ELSEWHERE))
        _wire(monkeypatch, repository, [])

        search.run_image(picture, None, [HERE.latitude, HERE.longitude], 500.0)

        out = capsys.readouterr().out
        assert "images/here.png" in out
        assert "images/far.png" not in out


class TestRunWithACircle:
    def test_only_images_inside_the_circle_come_back_with_their_position(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repository = _seeded(_image("here", HERE), _image("far", ELSEWHERE))
        _wire(monkeypatch, repository, [])

        search.run("a roof", None, [HERE.latitude, HERE.longitude], 500.0)

        out = capsys.readouterr().out
        assert "images/here.png" in out
        assert "-26.321400, -48.816300" in out
        assert "images/far.png" not in out
        ((_, _, filters),) = repository.search_similar_calls
        assert filters == SearchFilters(
            taken_within=GeoCircle(center=HERE, radius_m=500.0)
        )

    def test_it_says_how_many_images_the_circle_could_not_consider(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repository = _seeded(
            _image("here", HERE),
            _image("no-gps", source=PositionSource.UNKNOWN),
            _image("old"),
        )
        _wire(monkeypatch, repository, [])

        search.run("a roof", None, [HERE.latitude, HERE.longitude], 500.0)

        assert search.format_hidden_by_unknown_position(2) in capsys.readouterr().out

    def test_without_a_circle_the_count_is_neither_asked_nor_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repository = _seeded(_image("old"))
        _wire(monkeypatch, repository, [])

        search.run("a roof", None)

        assert "no known position" not in capsys.readouterr().out
        assert repository.count_unknown_position_calls == []

    def test_a_radius_below_the_floor_is_refused_with_the_drone_explained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API's policy, injected from settings: the CLI is no back door."""
        monkeypatch.setattr(settings, "min_radius_m", 300.0)
        repository = _seeded(_image("here", HERE))
        closed: list[bool] = []
        _wire(monkeypatch, repository, closed)

        with pytest.raises(SystemExit, match="drone"):
            search.run("a roof", None, [HERE.latitude, HERE.longitude], 50.0)

        assert repository.search_similar_calls == []
        assert closed == [True]

    def test_an_empty_query_is_refused_with_a_message_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, _seeded(), [])

        with pytest.raises(SystemExit, match="cannot be empty"):
            search.run("   ", None)
