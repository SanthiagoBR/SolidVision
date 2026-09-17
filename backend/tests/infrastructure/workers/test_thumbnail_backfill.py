"""The thumbnail backfill command (RFC-030 section 7.2).

The model-never-loads property is checked on the import graph in a fresh
interpreter, for the reason `test_capture_date_backfill.py` gives: a mock of
`encode_image` proves nobody called it, and passes just as happily with
`import torch` somewhere in the modules the command drags in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image as PILImage
from tests.application.fakes import (
    FakeDeviceRepository,
    FakeImageRepository,
    StubVolumeIdentityProvider,
    make_device,
)
from tests.infrastructure.workers.test_capture_date_backfill import (
    FORBIDDEN_MODULES,
    _every_import_in,
    _loaded_after_importing,
)

from app.application.use_cases.backfill_thumbnails import ThumbnailBackfillSummary
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.workers import thumbnail_backfill
from app.infrastructure.workers.thumbnail_backfill import _build_arg_parser, main

MODULE = "app.infrastructure.workers.thumbnail_backfill"


class TestTheModelIsNeverLoaded:
    def test_importing_the_command_loads_no_model_module(self) -> None:
        loaded = _loaded_after_importing([MODULE])

        assert MODULE in loaded
        for forbidden in FORBIDDEN_MODULES:
            assert forbidden not in loaded, f"importing {MODULE} loaded {forbidden}"

    def test_nothing_main_imports_loads_a_model_module_either(self) -> None:
        imports = _every_import_in(Path(thumbnail_backfill.__file__))
        assert any(name.endswith("thumbnail_generator") for name in imports)

        loaded = _loaded_after_importing(imports)

        assert "PIL" in loaded
        for forbidden in FORBIDDEN_MODULES:
            assert forbidden not in loaded, f"{MODULE}'s imports loaded {forbidden}"


class TestCommandLine:
    def test_the_root_is_required_and_has_no_default(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])
        (root,) = [a for a in _build_arg_parser()._actions if a.dest == "root"]
        assert root.default is None

    def test_only_missing_is_the_default_mode(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["--root", str(tmp_path)])

        assert args.force is False
        assert args.dry_run is False

    def test_the_two_modes_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(
                ["--root", str(tmp_path), "--force", "--only-missing"]
            )


class TestMainEndToEnd:
    """The real composition -- Pillow and the on-disk store included -- over fakes."""

    @pytest.fixture()
    def collection(self, tmp_path: Path) -> Path:
        root = tmp_path / "collection"
        root.mkdir()
        for name in ("old", "done"):
            PILImage.new("RGB", (800, 600), "olive").save(root / f"{name}.jpg")
        return root

    @pytest.fixture()
    def cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        directory = tmp_path / "cache"
        monkeypatch.setattr(settings, "thumbnail_directory", directory)
        return directory

    @pytest.fixture()
    def wired(
        self, collection: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> FakeImageRepository:
        device = make_device()
        repository = FakeImageRepository()
        for name, thumbnail in (("old", None), ("done", "zz/done.jpg")):
            relative = ImagePath(f"{name}.jpg")
            repository.save_indexed(
                IndexingRecord(
                    image=Image(
                        id=compute_image_id(device.id, relative),
                        device_id=device.id,
                        relative_path=relative,
                        filename=name,
                        extension="jpg",
                    ),
                    embedding=EmbeddingVector([0.1] * 8),
                    file_size=1,
                    file_modified_at=None,
                    thumbnail_path=thumbnail,
                )
            )
        repository.save_indexed_calls.clear()

        class _Session:
            def close(self) -> None:
                pass

        def no_model() -> None:
            raise AssertionError("the backfill must never compose the model")

        monkeypatch.setattr(
            "app.infrastructure.persistence.session.SessionLocal", lambda: _Session()
        )
        monkeypatch.setattr(
            "app.infrastructure.filesystem.volume_identity_provider."
            "WindowsVolumeIdentityProvider",
            lambda: StubVolumeIdentityProvider(
                identity=device.volume_identity, mount_point=collection
            ),
        )
        monkeypatch.setattr(
            "app.infrastructure.persistence.postgres_device_repository."
            "PostgresDeviceRepository",
            lambda session: FakeDeviceRepository([device]),
        )
        monkeypatch.setattr(
            "app.infrastructure.persistence.postgres_image_repository."
            "PostgresImageRepository",
            lambda session: repository,
        )
        monkeypatch.setattr(
            "app.presentation.dependencies.get_embedding_model", no_model
        )
        monkeypatch.setattr(
            "sys.argv", ["thumbnail_backfill", "--root", str(collection)]
        )
        return repository

    def _thumbnails(self, repository: FakeImageRepository) -> dict[str, str | None]:
        found = {}
        for image in repository.list():
            metadata = repository.get_index_metadata(image.id)
            assert metadata is not None
            found[image.filename] = metadata.thumbnail_path
        return found

    def test_rows_without_a_thumbnail_get_one_in_the_cache(
        self, wired: FakeImageRepository, cache: Path, collection: Path
    ) -> None:
        before = sorted(collection.rglob("*"))

        main()

        thumbnails = self._thumbnails(wired)
        assert thumbnails["done"] == "zz/done.jpg"
        old = thumbnails["old"]
        assert old is not None
        rendered = cache / old
        assert rendered.is_file()
        with PILImage.open(rendered) as picture:
            assert max(picture.size) == settings.thumbnail_max_edge
        assert sorted(collection.rglob("*")) == before
        assert wired.save_indexed_calls == []
        assert wired.save_indexed_many_calls == []

    def test_force_renders_the_row_that_already_had_one(
        self,
        wired: FakeImageRepository,
        cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", [*sys.argv, "--force"])

        main()

        thumbnails = self._thumbnails(wired)
        done = thumbnails["done"]
        assert done is not None and done != "zz/done.jpg"
        assert (cache / done).is_file()

    def test_a_dry_run_renders_nothing(
        self,
        wired: FakeImageRepository,
        cache: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", [*sys.argv, "--dry-run"])

        main()

        assert self._thumbnails(wired)["old"] is None
        assert not cache.exists()

    def test_a_cache_inside_the_root_is_never_walked(
        self,
        wired: FakeImageRepository,
        collection: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Run twice with the cache under `--root`: the second run sees two photos.

        Counted through the summary rather than inferred from the cache,
        because an unexcluded thumbnail has no row and would render nothing
        either way -- only `discovered` and `not_indexed` tell the two
        apart.
        """
        inside = collection / "cache"
        monkeypatch.setattr(settings, "thumbnail_directory", inside)
        reports: list[ThumbnailBackfillSummary] = []
        monkeypatch.setattr(thumbnail_backfill, "_log_summary", reports.append)

        main()
        main()

        assert len(list(inside.rglob("*.jpg"))) == 1
        second = reports[-1]
        assert second.discovered == 2
        assert second.not_indexed == 0
        assert second.already_generated == 2
