"""Typed loader and validator for a demo-dataset `manifest.json` (RFC-022 5.2).

Deliberately independent of the `app` package: this reads plain JSON and
raises `ManifestError` on any structural or referential violation, so a
broken manifest fails loudly here rather than surfacing as a confusing
error somewhere inside the indexing pipeline.

`relative_path` is the manifest's identity key, never a UUID -- an
`ImageId` is derived from an absolute, machine-specific path (see
`app.infrastructure.filesystem.image_identity`), so it is not stable
across checkouts and can never appear in a committed file. `expect` uses
the first-run outcome vocabulary defined in RFC-022 section 6.2:
`indexed` | `ignored` | `failed`.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
VALID_EXPECTATIONS = frozenset({"indexed", "ignored", "failed"})

DEMO_CORPUS_ROOT = Path(__file__).resolve().parents[1] / "dataset" / "demo"
DEMO_MANIFEST_PATH = DEMO_CORPUS_ROOT / "manifest.json"


class ManifestError(ValueError):
    """Raised when a manifest fails structural or referential validation."""


@dataclass(frozen=True)
class ManifestImage:
    """A single validated manifest entry."""

    relative_path: str
    collection: str
    file_modified_at: datetime.datetime
    caption: str
    tags: tuple[str, ...]
    source_url: str | None
    license: str
    attribution: str
    expect: str
    burst_group: str | None


@dataclass(frozen=True)
class Manifest:
    """A validated demo-dataset manifest."""

    version: int
    license_note: str
    images: tuple[ManifestImage, ...]

    def get(self, relative_path: str) -> ManifestImage:
        """Return the entry for `relative_path`, raising `KeyError` if absent."""
        for image in self.images:
            if image.relative_path == relative_path:
                return image
        raise KeyError(relative_path)

    def verify_matches_directory(
        self, corpus_root: Path, *, images_subdir: str = "images"
    ) -> None:
        """Raise `ManifestError` unless the manifest and the directory agree exactly.

        Checks both directions: every manifest entry must exist on disk,
        and every file under `corpus_root/images_subdir` must be described
        by the manifest. Guards against the drift risk in RFC-022 section 12.
        """
        missing = [
            image.relative_path
            for image in self.images
            if not (corpus_root / image.relative_path).is_file()
        ]
        if missing:
            raise ManifestError(
                f"{corpus_root}: manifest entries with no file on disk: {missing}"
            )

        images_root = corpus_root / images_subdir
        on_disk = (
            {
                path.relative_to(corpus_root).as_posix()
                for path in images_root.rglob("*")
                if path.is_file()
            }
            if images_root.is_dir()
            else set()
        )
        declared = {image.relative_path for image in self.images}
        untracked = sorted(on_disk - declared)
        if untracked:
            raise ManifestError(
                f"{corpus_root}: files on disk not described by the manifest: "
                f"{untracked}"
            )


def load_manifest(manifest_path: Path) -> Manifest:
    """Parse and validate `manifest_path`, raising `ManifestError` on any violation."""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"{manifest_path}: could not read file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{manifest_path}: invalid JSON: {exc}") from exc

    return _parse_manifest(raw, source=manifest_path)


def _parse_manifest(raw: Any, *, source: Path) -> Manifest:
    if not isinstance(raw, dict):
        raise ManifestError(f"{source}: manifest root must be a JSON object")

    version = raw.get("version")
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"{source}: unsupported manifest version {version!r}, "
            f"expected {MANIFEST_VERSION}"
        )

    license_note = raw.get("license_note")
    if not isinstance(license_note, str) or not license_note.strip():
        raise ManifestError(f"{source}: 'license_note' must be a non-empty string")

    raw_images = raw.get("images")
    if not isinstance(raw_images, list) or not raw_images:
        raise ManifestError(f"{source}: 'images' must be a non-empty list")

    images = tuple(
        _parse_image(entry, index=index, source=source)
        for index, entry in enumerate(raw_images)
    )

    seen_paths: set[str] = set()
    for image in images:
        if image.relative_path in seen_paths:
            raise ManifestError(
                f"{source}: duplicate relative_path {image.relative_path!r}"
            )
        seen_paths.add(image.relative_path)

    return Manifest(version=version, license_note=license_note, images=images)


def _parse_image(entry: Any, *, index: int, source: Path) -> ManifestImage:
    if not isinstance(entry, dict):
        raise ManifestError(f"{source}: images[{index}] must be a JSON object")

    def require_str(key: str) -> str:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(
                f"{source}: images[{index}].{key} must be a non-empty string"
            )
        return value

    def optional_str(key: str) -> str | None:
        value = entry.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(
                f"{source}: images[{index}].{key} must be a non-empty string or null"
            )
        return value

    relative_path = require_str("relative_path")
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise ManifestError(
            f"{source}: images[{index}].relative_path must be relative with no "
            f"'..' segments, got {relative_path!r}"
        )

    collection = require_str("collection")
    caption = require_str("caption")
    license_value = require_str("license")
    attribution = require_str("attribution")

    expect = require_str("expect")
    if expect not in VALID_EXPECTATIONS:
        raise ManifestError(
            f"{source}: images[{index}].expect must be one of "
            f"{sorted(VALID_EXPECTATIONS)}, got {expect!r}"
        )

    raw_tags = entry.get("tags")
    if not isinstance(raw_tags, list) or not all(
        isinstance(tag, str) for tag in raw_tags
    ):
        raise ManifestError(f"{source}: images[{index}].tags must be a list of strings")
    tags = tuple(raw_tags)

    raw_modified_at = entry.get("file_modified_at")
    if not isinstance(raw_modified_at, str):
        raise ManifestError(
            f"{source}: images[{index}].file_modified_at must be an ISO-8601 string"
        )
    try:
        file_modified_at = datetime.datetime.fromisoformat(
            raw_modified_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ManifestError(
            f"{source}: images[{index}].file_modified_at is not valid ISO-8601: "
            f"{raw_modified_at!r}"
        ) from exc
    if file_modified_at.tzinfo is None:
        raise ManifestError(
            f"{source}: images[{index}].file_modified_at must be timezone-aware"
        )

    source_url = optional_str("source_url")
    burst_group = optional_str("burst_group")

    if license_value != "proprietary-permitted" and source_url is None:
        raise ManifestError(
            f"{source}: images[{index}].source_url is required when license is "
            f"not 'proprietary-permitted' (got license={license_value!r})"
        )

    return ManifestImage(
        relative_path=relative_path,
        collection=collection,
        file_modified_at=file_modified_at,
        caption=caption,
        tags=tags,
        source_url=source_url,
        license=license_value,
        attribution=attribution,
        expect=expect,
        burst_group=burst_group,
    )
