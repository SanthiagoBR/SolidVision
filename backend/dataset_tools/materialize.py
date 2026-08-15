"""Materialize a manifest-described corpus into a target directory (RFC-022 4.2).

Git does not preserve mtimes: a fresh clone stamps every committed image
with checkout time, so any incremental-indexing test run directly against
`backend/dataset/demo/` would be non-reproducible -- whether a second run
skips a file would depend on filesystem timing rather than on the logic
under test (RFC-022 section 7.2). `materialize()` copies each manifest
entry into an isolated target root and then applies its authoritative
`file_modified_at` via `os.utime()`, so tests get a deterministic
filesystem state regardless of when or how the corpus was checked out.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dataset_tools.manifest import Manifest


def materialize(manifest: Manifest, corpus_root: Path, target_root: Path) -> Path:
    """Copy every file `manifest` describes from `corpus_root` into `target_root`.

    Applies each entry's `file_modified_at` to the copy via `os.utime()`
    after copying, overriding whatever mtime the copy itself produced.
    Directory structure below `target_root` mirrors each entry's
    `relative_path`. Returns `target_root` for convenient chaining.

    Raises `FileNotFoundError` via the underlying copy if a manifest entry
    has no corresponding file under `corpus_root` -- run
    `Manifest.verify_matches_directory()` first to fail with a clearer,
    aggregated error instead.
    """
    target_root.mkdir(parents=True, exist_ok=True)

    for image in manifest.images:
        source = corpus_root / image.relative_path
        destination = target_root / image.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

        timestamp = image.file_modified_at.timestamp()
        os.utime(destination, (timestamp, timestamp))

    return target_root
