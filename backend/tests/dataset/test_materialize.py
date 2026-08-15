"""Tests for `materialize()` (RFC-022 4.2 / 7.2): copying and mtime stamping."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest
from dataset_tools.materialize import materialize

VALID_ENTRY = {
    "relative_path": "images/rural/example.jpg",
    "collection": "demo-aerial",
    "file_modified_at": "2020-01-15T09:30:00Z",
    "caption": "An example",
    "tags": ["aerial"],
    "source_url": None,
    "license": "proprietary-permitted",
    "attribution": "the project's photographer",
    "expect": "indexed",
    "burst_group": None,
}


def _make_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal one-image corpus (manifest + source file) for isolated tests."""
    corpus_root = tmp_path / "source_corpus"
    (corpus_root / "images" / "rural").mkdir(parents=True)
    (corpus_root / "images" / "rural" / "example.jpg").write_bytes(b"fake-jpeg-bytes")

    manifest_path = corpus_root / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": 1, "license_note": "test", "images": [VALID_ENTRY]}),
        encoding="utf-8",
    )
    return corpus_root, manifest_path


def test_materialize_copies_the_file_content(tmp_path: Path) -> None:
    corpus_root, manifest_path = _make_corpus(tmp_path)
    manifest = load_manifest(manifest_path)

    target = materialize(manifest, corpus_root, tmp_path / "target")

    copied = target / "images" / "rural" / "example.jpg"
    assert copied.read_bytes() == b"fake-jpeg-bytes"


def test_materialize_stamps_the_manifest_mtime(tmp_path: Path) -> None:
    corpus_root, manifest_path = _make_corpus(tmp_path)
    manifest = load_manifest(manifest_path)

    target = materialize(manifest, corpus_root, tmp_path / "target")

    copied = target / "images" / "rural" / "example.jpg"
    expected = datetime.datetime(2020, 1, 15, 9, 30, 0, tzinfo=datetime.UTC).timestamp()
    assert copied.stat().st_mtime == expected


def test_materialize_ignores_the_source_files_checkout_mtime(tmp_path: Path) -> None:
    """The copy's mtime must come from the manifest, not from the source file.

    Simulates the exact failure mode RFC-022 section 7.2 describes: git
    does not preserve mtimes, so the source file here is given a
    deliberately wrong mtime before materializing, and the copy must not
    inherit it.
    """
    corpus_root, manifest_path = _make_corpus(tmp_path)
    source = corpus_root / "images" / "rural" / "example.jpg"
    wrong_timestamp = datetime.datetime(1999, 1, 1, tzinfo=datetime.UTC).timestamp()
    os.utime(source, (wrong_timestamp, wrong_timestamp))

    manifest = load_manifest(manifest_path)
    target = materialize(manifest, corpus_root, tmp_path / "target")

    copied = target / "images" / "rural" / "example.jpg"
    assert copied.stat().st_mtime != wrong_timestamp


def test_materialize_returns_the_target_root(tmp_path: Path) -> None:
    corpus_root, manifest_path = _make_corpus(tmp_path)
    manifest = load_manifest(manifest_path)

    target_root = tmp_path / "target"
    result = materialize(manifest, corpus_root, target_root)

    assert result == target_root


def test_materialize_is_safe_to_rerun_into_the_same_target(tmp_path: Path) -> None:
    corpus_root, manifest_path = _make_corpus(tmp_path)
    manifest = load_manifest(manifest_path)
    target_root = tmp_path / "target"

    materialize(manifest, corpus_root, target_root)
    materialize(manifest, corpus_root, target_root)  # must not raise

    copied = target_root / "images" / "rural" / "example.jpg"
    assert copied.read_bytes() == b"fake-jpeg-bytes"


def test_materialize_against_the_real_demo_manifest(tmp_path: Path) -> None:
    """Integration check: the real committed corpus materializes cleanly."""
    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)

    target = materialize(manifest, DEMO_CORPUS_ROOT, tmp_path / "demo_corpus")

    for image in manifest.images:
        copied = target / image.relative_path
        assert copied.is_file()
        assert copied.stat().st_size > 0
        assert copied.stat().st_mtime == image.file_modified_at.timestamp()
