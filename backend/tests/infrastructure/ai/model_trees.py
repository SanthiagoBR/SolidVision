"""Model trees in miniature, with a lockfile of their own (RFC-035 section 9).

The real tree is 924 MB and is not in the suite's way: these trees hold a few
hundred bytes per model, in the same layout -- a manifest naming the lockfile,
one directory per model -- and a lockfile that describes exactly them. What
the code under test does with a tree does not depend on its size.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.infrastructure.ai.model_runtime import (
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    Lockfile,
    ModelRuntime,
)
from app.infrastructure.config.layout import Environment

CONTENTS: dict[str, dict[str, bytes]] = {
    "clip": {
        "config.json": b'{"model_type": "clip"}',
        "model.safetensors": b"the weights of a very small clip " * 40,
        "tokenizer.json": b'{"vocab": {}}',
    },
    "translator": {
        "config.json": b'{"model_type": "marian"}',
        "model.safetensors": b"the converted weights of a translator " * 40,
        "source.spm": b"sentencepiece",
    },
}
"""Two models named like the real ones, and one weights file of each kind."""

SECRET = b"the weights of a very small clip"
"""Part of a file's content, which no message may ever repeat."""


@dataclass(frozen=True)
class Tree:
    root: Path
    lockfile: Lockfile
    lockfile_bytes: bytes

    def directory(self, key: str) -> Path:
        return self.root / self.lockfile.models[key].directory

    def file(self, key: str, name: str) -> Path:
        return self.directory(key) / name

    def runtime(
        self,
        environment: Environment = Environment.DEVELOPMENT,
        record_file: Path | None = None,
        offline: bool = True,
    ) -> ModelRuntime:
        return ModelRuntime(
            self.root,
            self.lockfile,
            environment=environment,
            record_file=record_file,
            offline=lambda: offline,
        )


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def lock_for(contents: dict[str, dict[str, bytes]]) -> bytes:
    """The bytes of a lockfile that describes `contents` exactly."""
    lock = {
        key: {
            "repository": f"example/{key}",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "directory": f"{key}-model",
            "license": "mit",
            "files": {
                name: {"size": len(content), "sha256": sha256(content)}
                for name, content in sorted(files.items())
            },
        }
        for key, files in contents.items()
    }
    return (json.dumps(lock, indent=2) + "\n").encode("utf-8")


def make_tree(root: Path, contents: dict[str, dict[str, bytes]] | None = None) -> Tree:
    """Write a complete tree at `root`, and return it with its lockfile."""
    contents = CONTENTS if contents is None else contents
    lockfile_bytes = lock_for(contents)
    lockfile = Lockfile.parse(lockfile_bytes)
    for key, files in contents.items():
        directory = root / lockfile.models[key].directory
        directory.mkdir(parents=True)
        for name, content in files.items():
            (directory / name).write_bytes(content)
    write_manifest(root, lockfile.sha256)
    return Tree(root, lockfile, lockfile_bytes)


def write_manifest(root: Path, lockfile_sha256: str) -> None:
    manifest = {"format": MANIFEST_FORMAT, "lockfile_sha256": lockfile_sha256}
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def flip_one_byte(path: Path) -> None:
    """Corrupt a file without changing its size."""
    content = bytearray(path.read_bytes())
    content[len(content) // 2] ^= 0x01
    path.write_bytes(bytes(content))
