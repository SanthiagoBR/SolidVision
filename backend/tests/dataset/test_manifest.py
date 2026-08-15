"""Tests for the manifest loader/validator (RFC-022 5.2) and the real manifest."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from dataset_tools.manifest import (
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    ManifestError,
    load_manifest,
)

VALID_ENTRY: dict[str, Any] = {
    "relative_path": "images/aerial/rural/example.jpg",
    "collection": "demo-aerial",
    "file_modified_at": "2024-06-01T12:00:00Z",
    "caption": "An example rural property",
    "tags": ["aerial", "rural"],
    "source_url": None,
    "license": "proprietary-permitted",
    "attribution": "the project's photographer",
    "expect": "indexed",
    "burst_group": None,
}


def _write_manifest(tmp_path: Path, *, images: list[dict[str, Any]]) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": 1, "license_note": "test", "images": images}),
        encoding="utf-8",
    )
    return manifest_path


class TestRealDemoManifest:
    """The manifest actually committed at `backend/dataset/demo/manifest.json`."""

    def test_loads_without_error(self) -> None:
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        assert len(manifest.images) == 45

    def test_matches_the_directory_exactly(self) -> None:
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        manifest.verify_matches_directory(DEMO_CORPUS_ROOT)

    def test_every_relative_path_is_unique(self) -> None:
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        paths = [image.relative_path for image in manifest.images]
        assert len(paths) == len(set(paths))

    def test_get_returns_a_known_entry(self) -> None:
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        image = manifest.get("images/aerial/rural/lake_property_01.jpg")
        assert "lake" in image.tags

    def test_get_raises_key_error_for_unknown_path(self) -> None:
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        with pytest.raises(KeyError):
            manifest.get("images/does/not/exist.jpg")


class TestLoadManifestStructuralValidation:
    def test_missing_file_raises_manifest_error(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="could not read file"):
            load_manifest(tmp_path / "does_not_exist.json")

    def test_invalid_json_raises_manifest_error(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ManifestError, match="invalid JSON"):
            load_manifest(manifest_path)

    def test_non_object_root_raises_manifest_error(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ManifestError, match="must be a JSON object"):
            load_manifest(manifest_path)

    def test_wrong_version_raises_manifest_error(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps({"version": 2, "license_note": "x", "images": [VALID_ENTRY]}),
            encoding="utf-8",
        )
        with pytest.raises(ManifestError, match="unsupported manifest version"):
            load_manifest(manifest_path)

    def test_empty_images_list_raises_manifest_error(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, images=[])
        with pytest.raises(ManifestError, match="non-empty list"):
            load_manifest(manifest_path)

    def test_duplicate_relative_path_raises_manifest_error(
        self, tmp_path: Path
    ) -> None:
        manifest_path = _write_manifest(
            tmp_path, images=[VALID_ENTRY, dict(VALID_ENTRY)]
        )
        with pytest.raises(ManifestError, match="duplicate relative_path"):
            load_manifest(manifest_path)


class TestLoadManifestEntryValidation:
    def test_missing_required_field_raises_manifest_error(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY)
        del entry["caption"]
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="caption"):
            load_manifest(manifest_path)

    def test_path_traversal_is_rejected(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, relative_path="images/../../etc/passwd")
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="relative_path"):
            load_manifest(manifest_path)

    def test_absolute_path_is_rejected(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, relative_path="/etc/passwd")
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="relative_path"):
            load_manifest(manifest_path)

    def test_invalid_expect_value_is_rejected(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, expect="skipped")
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="expect"):
            load_manifest(manifest_path)

    def test_non_iso_file_modified_at_is_rejected(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, file_modified_at="not-a-date")
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="file_modified_at"):
            load_manifest(manifest_path)

    def test_naive_file_modified_at_is_rejected(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, file_modified_at="2024-06-01T12:00:00")
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="timezone-aware"):
            load_manifest(manifest_path)

    def test_tags_must_be_a_list_of_strings(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, tags="aerial")
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="tags"):
            load_manifest(manifest_path)

    def test_non_proprietary_license_requires_source_url(self, tmp_path: Path) -> None:
        entry = dict(VALID_ENTRY, license="CC-BY-4.0", source_url=None)
        manifest_path = _write_manifest(tmp_path, images=[entry])
        with pytest.raises(ManifestError, match="source_url is required"):
            load_manifest(manifest_path)

    def test_cc_licensed_entry_with_source_url_is_accepted(
        self, tmp_path: Path
    ) -> None:
        entry = dict(
            VALID_ENTRY,
            license="CC-BY-4.0",
            source_url="https://commons.wikimedia.org/wiki/File:example.jpg",
            attribution="Someone",
        )
        manifest_path = _write_manifest(tmp_path, images=[entry])
        manifest = load_manifest(manifest_path)
        assert manifest.images[0].source_url is not None

    def test_valid_entry_parses_expected_types(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, images=[VALID_ENTRY])
        manifest = load_manifest(manifest_path)
        image = manifest.images[0]
        assert image.tags == ("aerial", "rural")
        assert image.file_modified_at == datetime.datetime(
            2024, 6, 1, 12, 0, 0, tzinfo=datetime.UTC
        )
        assert image.source_url is None
        assert image.burst_group is None


class TestVerifyMatchesDirectory:
    def test_raises_when_manifest_entry_has_no_file_on_disk(
        self, tmp_path: Path
    ) -> None:
        manifest_path = _write_manifest(tmp_path, images=[VALID_ENTRY])
        manifest = load_manifest(manifest_path)
        with pytest.raises(ManifestError, match="no file on disk"):
            manifest.verify_matches_directory(tmp_path)

    def test_raises_when_disk_has_an_untracked_file(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, images=[VALID_ENTRY])
        manifest = load_manifest(manifest_path)

        tracked = tmp_path / VALID_ENTRY["relative_path"]
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_bytes(b"fake-jpeg-bytes")

        untracked = tracked.parent / "not_in_manifest.jpg"
        untracked.write_bytes(b"fake-jpeg-bytes")

        with pytest.raises(ManifestError, match="not described by the manifest"):
            manifest.verify_matches_directory(tmp_path)

    def test_passes_when_manifest_and_directory_agree(self, tmp_path: Path) -> None:
        manifest_path = _write_manifest(tmp_path, images=[VALID_ENTRY])
        manifest = load_manifest(manifest_path)

        tracked = tmp_path / VALID_ENTRY["relative_path"]
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_bytes(b"fake-jpeg-bytes")

        manifest.verify_matches_directory(tmp_path)
