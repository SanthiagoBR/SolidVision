"""The capture-date backfill command (RFC-028 section 7).

The property the RFC sells -- dates for an indexed disk without the model --
is checked here against the *import graph*, in a fresh interpreter, rather than
by mocking the model and observing that nobody called it. A mock proves that
`encode_image` was not invoked; it passes just as happily with `import torch`
at the top of this module or of anything it imports, which is where the real
cost of "loading the model" lives: seconds of import and hundreds of megabytes
before a single file is read.
"""

from __future__ import annotations

import ast
import datetime
import json
import subprocess
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

from app.domain.entities.image import Image
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.filesystem.exif_capture_date import (
    DATE_TIME_ORIGINAL,
    EXIF_IFD_POINTER,
)
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.workers import capture_date_backfill
from app.infrastructure.workers.capture_date_backfill import _build_arg_parser, main

BACKEND_DIR = Path(__file__).resolve().parents[3]
MODULE = "app.infrastructure.workers.capture_date_backfill"
SHOT = datetime.datetime(2018, 7, 14, 15, 32, 5)

FORBIDDEN_MODULES = (
    "torch",
    "transformers",
    "langdetect",
    "app.infrastructure.ai.clip_embedding_model",
    "app.infrastructure.ai.query_translator",
    "app.presentation.dependencies",
)
"""What "loading the model" means in this codebase, module by module.

`app.presentation.dependencies` is on the list because it is the one place
the CLIP adapter is composed; importing it is the first step to building
the model even before torch appears.
"""


def _loaded_after_importing(modules: list[str]) -> set[str]:
    """Import `modules` in a fresh interpreter and return every module loaded.

    A subprocess, because this test process has long since imported torch
    through other test files, so `sys.modules` here says nothing about what
    the backfill pulls in.
    """
    script = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


def _every_import_in(module_path: Path) -> list[str]:
    """Every module the file imports, at module level or inside a function."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return sorted(set(names))


class TestTheModelIsNeverLoaded:
    """RFC-028 section 7, proven on the import graph rather than by a mock."""

    def test_importing_the_command_loads_no_model_module(self) -> None:
        loaded = _loaded_after_importing([MODULE])

        assert MODULE in loaded
        for forbidden in FORBIDDEN_MODULES:
            assert forbidden not in loaded, f"importing {MODULE} loaded {forbidden}"

    def test_nothing_main_imports_loads_a_model_module_either(self) -> None:
        """`main()`'s function-local imports included, transitively.

        Those imports only run when the command does, so the module-level
        check above cannot see them -- and they are exactly where a
        copy-paste from `indexing_worker.main()` would bring in
        `get_embedding_model`.
        """
        imports = _every_import_in(Path(capture_date_backfill.__file__))
        assert any(name.endswith("postgres_image_repository") for name in imports)

        loaded = _loaded_after_importing(imports)

        for forbidden in FORBIDDEN_MODULES:
            assert forbidden not in loaded, f"{MODULE}'s imports loaded {forbidden}"

    def test_the_guard_can_fail(self) -> None:
        """Guards the guard: the indexing worker's own imports *do* load the model.

        If this ever stops being true, the two checks above prove nothing,
        because the subprocess would not be seeing model imports at all.
        """
        worker = Path(capture_date_backfill.__file__).with_name("indexing_worker.py")

        loaded = _loaded_after_importing(_every_import_in(worker))

        assert "app.presentation.dependencies" in loaded
        assert "torch" in loaded


class TestCommandLine:
    def test_the_root_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args([])

    def test_the_root_has_no_default(self) -> None:
        (root,) = [a for a in _build_arg_parser()._actions if a.dest == "root"]

        assert root.default is None
        assert root.required is True

    def test_only_unknown_is_the_default_mode(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["--root", str(tmp_path)])

        assert args.force is False
        assert args.dry_run is False
        assert args.label == ""

    def test_only_unknown_can_be_stated_explicitly(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(
            ["--root", str(tmp_path), "--only-unknown"]
        )

        assert args.force is False

    def test_force_is_a_flag(self, tmp_path: Path) -> None:
        args = _build_arg_parser().parse_args(["--root", str(tmp_path), "--force"])

        assert args.force is True

    def test_the_two_modes_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(
                ["--root", str(tmp_path), "--force", "--only-unknown"]
            )


def _write_jpeg(path: Path, date_time_original: str | None) -> None:
    exif = PILImage.Exif()
    if date_time_original is not None:
        exif.get_ifd(EXIF_IFD_POINTER)[DATE_TIME_ORIGINAL] = date_time_original
    PILImage.new("RGB", (8, 8)).save(path, "JPEG", exif=exif.tobytes())


class TestMainEndToEnd:
    """The real composition, with the session, volume and repositories stubbed."""

    @pytest.fixture()
    def wired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> FakeImageRepository:
        device = make_device()
        repository = FakeImageRepository()
        for name in ("dated", "undated"):
            _write_jpeg(
                tmp_path / f"{name}.jpg",
                "2018:07:14 15:32:05" if name == "dated" else None,
            )
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
                identity=device.volume_identity, mount_point=tmp_path
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
            "sys.argv", ["capture_date_backfill", "--root", str(tmp_path)]
        )
        return repository

    def test_indexed_files_get_their_dates_and_no_embedding_is_touched(
        self, wired: FakeImageRepository
    ) -> None:
        main()

        dates = {image.filename: image.capture_date for image in wired.list()}
        assert dates == {
            "dated": CaptureDate(SHOT, CaptureSource.EXIF_ORIGINAL),
            "undated": CaptureDate.unknown(),
        }
        assert wired.save_indexed_calls == []
        assert wired.save_indexed_many_calls == []

    def test_a_dry_run_writes_nothing(
        self, wired: FakeImageRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", [*sys.argv, "--dry-run"])

        main()

        assert all(image.capture_source is None for image in wired.list())
        assert wired.update_capture_date_many_calls == []
