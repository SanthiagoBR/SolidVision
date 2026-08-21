"""Tests for the streaming SHA-256 content hasher (RFC-024 section 4)."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.sha256_content_hasher import (
    READ_CHUNK_BYTES,
    Sha256ContentHasher,
)


def _image(path: Path) -> Image:
    image_path = ImagePath(str(path))
    return Image(
        id=ImageId(uuid.uuid4()),
        path=image_path,
        filename=path.stem,
        extension=path.suffix.lstrip(".").lower(),
    )


def test_it_implements_the_content_hasher_port() -> None:
    assert isinstance(Sha256ContentHasher(), ContentHasherPort)


def test_the_digest_matches_hashlib_over_the_whole_file(tmp_path: Path) -> None:
    payload = b"some bytes that stand in for a photo"
    path = tmp_path / "photo.jpg"
    path.write_bytes(payload)

    digest = Sha256ContentHasher().hash_image(_image(path))

    assert digest == hashlib.sha256(payload).hexdigest()


def test_the_digest_is_hex_encoded_and_sixty_four_characters(tmp_path: Path) -> None:
    """The column is `varchar(64)`; a longer encoding would not fit."""
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"content")

    digest = Sha256ContentHasher().hash_image(_image(path))

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_an_empty_file_hashes_rather_than_failing(tmp_path: Path) -> None:
    """`zero_byte.jpg` is a real RFC-022 hard case and must reach the decoder.

    Hashing runs before inference, so a hasher that treated an empty file as
    an error would change that case's outcome from "failed to decode" to
    "failed to hash", losing the real reason.
    """
    path = tmp_path / "zero_byte.jpg"
    path.write_bytes(b"")

    assert (
        Sha256ContentHasher().hash_image(_image(path))
        == hashlib.sha256(b"").hexdigest()
    )


def test_identical_bytes_at_two_paths_hash_identically(tmp_path: Path) -> None:
    """The property the whole change-detection scheme rests on."""
    payload = b"byte identical"
    first = tmp_path / "a.jpg"
    second = tmp_path / "b.jpg"
    first.write_bytes(payload)
    second.write_bytes(payload)

    hasher = Sha256ContentHasher()

    assert hasher.hash_image(_image(first)) == hasher.hash_image(_image(second))


def test_a_single_changed_byte_changes_the_digest(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"aaaaaaaa")
    hasher = Sha256ContentHasher()
    before = hasher.hash_image(_image(path))

    path.write_bytes(b"aaaabaaa")

    assert hasher.hash_image(_image(path)) != before


def test_a_missing_file_raises_rather_than_returning_a_sentinel(
    tmp_path: Path,
) -> None:
    """Callers isolate failures per file; a sentinel would hide one silently."""
    with pytest.raises(FileNotFoundError):
        Sha256ContentHasher().hash_image(_image(tmp_path / "absent.jpg"))


class TestStreaming:
    """RFC-024 section 4: hashing must never load a whole file into memory."""

    def test_reads_are_bounded_rather_than_slurping_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `read()` with no bound is the bug this test exists to catch.

        `Path.read_bytes()` and `stream.read()` both produce a correct
        digest, so no assertion about the *result* can tell them apart from
        the chunked loop. The only observable difference is the size asked
        for, so that is what is recorded.
        """
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"x" * (READ_CHUNK_BYTES * 2 + 17))

        requested: list[int | None] = []
        real_open = Path.open

        class _RecordingStream:
            def __init__(self, wrapped: Any) -> None:
                self._wrapped = wrapped

            def read(self, size: int | None = None) -> bytes:
                requested.append(size)
                chunk: bytes = self._wrapped.read(size)
                return chunk

            def __enter__(self) -> _RecordingStream:
                return self

            def __exit__(self, *exc_info: object) -> None:
                self._wrapped.close()

        def recording_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            return _RecordingStream(real_open(self, *args, **kwargs))

        monkeypatch.setattr(Path, "open", recording_open)

        Sha256ContentHasher().hash_image(_image(path))

        assert requested, "the file was never read"
        assert all(size == READ_CHUNK_BYTES for size in requested)
        # Three full-size requests plus the final empty one that ends the loop.
        assert len(requested) == 4

    def test_a_file_larger_than_one_chunk_still_hashes_correctly(
        self, tmp_path: Path
    ) -> None:
        """Chunking must not drop or duplicate a boundary byte."""
        payload = bytes(index % 256 for index in range(READ_CHUNK_BYTES + 1000))
        path = tmp_path / "large.jpg"
        path.write_bytes(payload)

        digest = Sha256ContentHasher().hash_image(_image(path))

        assert digest == hashlib.sha256(payload).hexdigest()
