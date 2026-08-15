"""Tests for the scale-corpus generator (RFC-022 section 7.4 / deliverable 5).

Uses small counts throughout -- this is a unit-test suite, not the 100k
benchmark itself, which RFC-022 section 14 explicitly defers.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage

from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from dataset_tools.generators.scale_corpus import IMAGE_SIZE, generate


def test_generates_exactly_count_files(tmp_path: Path) -> None:
    generate(tmp_path, 10)

    files = list(tmp_path.rglob("*.jpg"))
    assert len(files) == 10


def test_generated_files_are_valid_jpegs_of_the_expected_size(
    tmp_path: Path,
) -> None:
    generate(tmp_path, 3)

    for file in tmp_path.rglob("*.jpg"):
        with PILImage.open(file) as image:
            image.verify()
        with PILImage.open(file) as image:
            assert image.size == IMAGE_SIZE
            assert image.format == "JPEG"


def test_output_is_sharded_across_subdirectories(tmp_path: Path) -> None:
    generate(tmp_path, 12, shard_size=5)

    shards = sorted(path.name for path in tmp_path.iterdir() if path.is_dir())
    assert shards == ["00000", "00001", "00002"]

    counts = [len(list((tmp_path / shard).glob("*.jpg"))) for shard in shards]
    assert counts == [5, 5, 2]


def test_generation_is_deterministic_across_runs(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    generate(first_root, 5)
    generate(second_root, 5)

    first_files = sorted(first_root.rglob("*.jpg"))
    second_files = sorted(second_root.rglob("*.jpg"))

    assert len(first_files) == len(second_files) == 5
    for first_file, second_file in zip(first_files, second_files, strict=True):
        assert first_file.read_bytes() == second_file.read_bytes()


def test_generated_corpus_is_discoverable_by_the_real_provider(
    tmp_path: Path,
) -> None:
    """The sharded output must be consumable by the actual production pipeline."""
    generate(tmp_path, 25, shard_size=10)

    provider = FilesystemImageProvider(tmp_path, SUPPORTED_IMAGE_EXTENSIONS)
    discovered = list(provider.discover())

    assert len(discovered) == 25
    assert all(item.extension == "jpg" for item in discovered)
