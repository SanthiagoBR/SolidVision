"""`scripts/vendor_models.py` makes the tree the app accepts, or none (RFC-035 4.3).

Nothing here reaches the network. The "Hub" is a directory, laid out like the
cache the script reads with `--from-cache` or handed to it as a fetch
function, and the lockfiles are the miniature ones of `model_trees.py`. What
holds the script and the app together is tested by building a tree with one
and checking it with the other: the script cannot import the app's package,
which turns the offline mode on (section 4.3), so the two share no code.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.infrastructure.ai import model_runtime
from app.infrastructure.ai.model_runtime import CLIP, TRANSLATOR, Lockfile
from app.infrastructure.config.layout import Environment
from tests.infrastructure.ai.model_trees import CONTENTS, lock_for

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "vendor_models.py"


@pytest.fixture(scope="module")
def vendor() -> ModuleType:
    """The script, loaded from its file: `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("vendor_models", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def lock_bytes() -> bytes:
    return lock_for(CONTENTS)


@pytest.fixture()
def lock(lock_bytes: bytes) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(lock_bytes)
    return parsed


@pytest.fixture()
def cache(tmp_path: Path, lock: dict[str, Any]) -> Path:
    """A Hugging Face cache holding the pinned snapshot of each model."""
    root = tmp_path / "hf-cache" / "hub"
    for key, model in lock.items():
        snapshot = (
            root
            / ("models--" + model["repository"].replace("/", "--"))
            / "snapshots"
            / model["revision"]
        )
        snapshot.mkdir(parents=True)
        for name, content in CONTENTS[key].items():
            (snapshot / name).write_bytes(content)
    return root


def build(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    target: Path,
    fetch: Any,
) -> dict[str, Any]:
    manifest: dict[str, Any] = vendor.materialize(
        lock, vendor.lockfile_digest(lock_bytes), target, fetch, "test"
    )
    return manifest


def leftovers(target: Path) -> list[str]:
    return sorted(path.name for path in target.parent.iterdir())


# --------------------------------------------------------------------------
# A tree the app accepts
# --------------------------------------------------------------------------


def test_the_tree_is_the_lockfile_and_the_app_accepts_it(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime" / "models"

    manifest = build(vendor, lock, lock_bytes, target, vendor.from_cache(cache))

    runtime = model_runtime.ModelRuntime(
        target,
        Lockfile.parse(lock_bytes),
        Environment.DEVELOPMENT,
        None,
        offline=lambda: True,
    )
    runtime.check_presence()
    assert runtime.verified_directory(CLIP) == target / "clip-model"
    assert runtime.verified_directory(TRANSLATOR) == target / "translator-model"
    assert manifest["bytes"] == sum(
        len(content) for files in CONTENTS.values() for content in files.values()
    )
    assert leftovers(target) == ["models"], "no staging, no retired tree"


def test_nothing_but_the_files_of_the_lockfile_goes_in(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    """Section 4.2: plain files, no `blobs`, no links, no `refs` -- and not
    the other files a snapshot may hold."""
    snapshot = next(cache.glob("models--example--clip/snapshots/*"))
    (snapshot / "open_clip_pytorch_model.bin").write_bytes(b"not in the lockfile")
    target = tmp_path / "models"

    build(vendor, lock, lock_bytes, target, vendor.from_cache(cache))

    found = sorted(
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file()
    )
    assert found == sorted(
        [vendor.MANIFEST_NAME]
        + [
            f"{lock[key]['directory']}/{name}"
            for key, files in CONTENTS.items()
            for name in files
        ]
    )


def test_the_script_and_the_app_agree_on_the_manifest_and_the_digest(
    vendor: ModuleType, lock_bytes: bytes
) -> None:
    crlf = lock_bytes.replace(b"\n", b"\r\n")

    assert vendor.MANIFEST_NAME == model_runtime.MANIFEST_NAME
    assert vendor.MANIFEST_FORMAT == model_runtime.MANIFEST_FORMAT
    assert vendor.lockfile_digest(crlf) == model_runtime.lockfile_digest(lock_bytes)


def test_the_pins_of_the_script_are_the_lockfiles(vendor: ModuleType) -> None:
    """`--lock` writes the lockfile from `PINNED`; the two must not drift."""
    real = json.loads(vendor.LOCKFILE.read_text(encoding="utf-8"))

    assert {
        key: (model["repository"], model["revision"], model["directory"])
        for key, model in real.items()
    } == vendor.PINNED
    assert {
        key: {
            made: (recipe["from"], tuple(recipe["tied"]))
            for made, recipe in model["converted"].items()
        }
        for key, model in real.items()
        if "converted" in model
    } == vendor.CONVERSIONS


def test_the_lockfile_the_script_reads_is_the_one_the_app_reads(
    vendor: ModuleType,
) -> None:
    assert vendor.LOCKFILE.name == model_runtime.LOCKFILE_NAME
    assert vendor.read_lock(vendor.LOCKFILE)
    assert vendor.lockfile_digest(vendor.LOCKFILE.read_bytes()) == (
        model_runtime.read_lockfile().sha256
    )


# --------------------------------------------------------------------------
# Nothing half-built
# --------------------------------------------------------------------------


def test_an_interrupted_materialization_leaves_no_tree(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    fetched: list[str] = []
    from_cache = vendor.from_cache(cache)

    def interrupted(repository: str, revision: str, name: str) -> Path:
        fetched.append(name)
        if len(fetched) == 3:
            raise KeyboardInterrupt
        path: Path = from_cache(repository, revision, name)
        return path

    target = tmp_path / "runtime" / "models"

    with pytest.raises(KeyboardInterrupt):
        build(vendor, lock, lock_bytes, target, interrupted)

    assert not target.exists()
    assert leftovers(target) == []


def test_a_failed_materialization_keeps_the_tree_that_was_there(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime" / "models"
    build(vendor, lock, lock_bytes, target, vendor.from_cache(cache))
    before = (target / vendor.MANIFEST_NAME).read_bytes()

    def failing(repository: str, revision: str, name: str) -> Path:
        raise vendor.VendorError("the Hub is away")

    with pytest.raises(vendor.VendorError, match="away"):
        build(vendor, lock, lock_bytes, target, failing)

    assert (target / vendor.MANIFEST_NAME).read_bytes() == before
    assert leftovers(target) == ["models"]


def test_a_file_from_the_hub_with_the_wrong_hash_is_refused(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    tmp_path: Path,
) -> None:
    """The origin does not matter, the hash decides (section 4.3)."""
    hub = tmp_path / "hub"
    hub.mkdir()

    def fake_hub(repository: str, revision: str, name: str) -> Path:
        path = hub / name
        content = CONTENTS["clip" if repository.endswith("clip") else "translator"][
            name
        ]
        path.write_bytes(content.replace(b"weights", b"WEIGHTS"))
        return path

    target = tmp_path / "models"

    with pytest.raises(
        vendor.VendorError, match=r"model\.safetensors: sha256 .*lockfile"
    ):
        build(vendor, lock, lock_bytes, target, fake_hub)

    assert not target.exists()


def test_from_the_cache_a_file_with_the_wrong_hash_is_refused(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    snapshot = next(cache.glob("models--example--translator/snapshots/*"))
    (snapshot / "model.safetensors").write_bytes(
        b"x" * len(CONTENTS["translator"]["model.safetensors"])
    )

    with pytest.raises(vendor.VendorError, match="model.safetensors: sha256"):
        build(vendor, lock, lock_bytes, tmp_path / "models", vendor.from_cache(cache))

    assert not (tmp_path / "models").exists()


def test_from_the_cache_a_truncated_file_is_refused_by_its_size(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    snapshot = next(cache.glob("models--example--clip/snapshots/*"))
    (snapshot / "model.safetensors").write_bytes(b"short")

    with pytest.raises(vendor.VendorError, match="5 bytes, the lockfile says"):
        build(vendor, lock, lock_bytes, tmp_path / "models", vendor.from_cache(cache))


def test_from_the_cache_a_missing_snapshot_says_to_download_instead(
    vendor: ModuleType,
    lock: dict[str, Any],
    lock_bytes: bytes,
    cache: Path,
    tmp_path: Path,
) -> None:
    snapshot = next(cache.glob("models--example--clip/snapshots/*"))
    (snapshot / "tokenizer.json").unlink()

    with pytest.raises(vendor.VendorError, match="Run without --from-cache"):
        build(vendor, lock, lock_bytes, tmp_path / "models", vendor.from_cache(cache))


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"clip": {"files": {}}}',
        '{"clip": {"repository": "r", "revision": "0", "directory": "d", '
        '"files": {"f": {"size": 1}}}}',
        "not json",
        '{"t": {"repository": "r", "revision": "0", "directory": "d", '
        '"files": {"f": {"size": 1, "sha256": "' + "0" * 64 + '"}}, '
        '"converted": {"g": {"from": "p", "size": 1, "sha256": "' + "0" * 64 + '"}}}}',
        '{"t": {"repository": "r", "revision": "0", "directory": "d", '
        '"files": {"f": {"size": 1, "sha256": "' + "0" * 64 + '"}}, '
        '"converted": {"f": {"size": 1, "sha256": "' + "0" * 64 + '"}}}}',
    ],
)
def test_a_lockfile_without_what_the_app_relies_on_is_refused(
    vendor: ModuleType, tmp_path: Path, content: str
) -> None:
    lockfile = tmp_path / "models.lock.json"
    lockfile.write_text(content, encoding="utf-8")

    with pytest.raises(vendor.VendorError):
        vendor.read_lock(lockfile)


def test_the_git_blob_id_is_what_git_calls_the_content(
    vendor: ModuleType, tmp_path: Path
) -> None:
    """`--lock`'s second source for a small file: `git hash-object`."""
    path = tmp_path / "config.json"
    path.write_bytes(b"hello\n")

    assert vendor.git_blob_id(path) == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert vendor.file_sha256(path) == hashlib.sha256(b"hello\n").hexdigest()


# --------------------------------------------------------------------------
# The conversion of the translator's pickle (section 7.4, second branch)
# --------------------------------------------------------------------------

TIED = ("model.decoder.embed_tokens.weight", "model.encoder.embed_tokens.weight")


@pytest.fixture()
def pickled(tmp_path: Path) -> Path:
    """A pickle shaped like the translator's: three names, one tensor."""
    import torch

    shared = torch.arange(12, dtype=torch.float32).reshape(3, 4)
    state = {
        "final_logits_bias": torch.zeros(1, 3),
        "model.decoder.embed_tokens.weight": shared,
        "model.encoder.embed_tokens.weight": shared,
        "model.layers.0.weight": torch.ones(2, 2),
        "model.shared.weight": shared,
    }
    path = tmp_path / "pickle" / "pytorch_model.bin"
    path.parent.mkdir()
    torch.save(state, path)
    return path


def metadata_of(path: Path) -> Any:
    size = int.from_bytes(path.read_bytes()[:8], "little")
    return json.loads(path.read_bytes()[8 : 8 + size]).get("__metadata__")


def test_the_conversion_keeps_one_name_per_tensor_and_every_value(
    vendor: ModuleType, pickled: Path, tmp_path: Path
) -> None:
    import torch
    from safetensors.torch import load_file

    target = tmp_path / "model.safetensors"
    vendor.convert_pickle(pickled, target, TIED)

    written = load_file(str(target))
    assert sorted(written) == [
        "final_logits_bias",
        "model.layers.0.weight",
        "model.shared.weight",
    ]
    assert torch.equal(written["model.shared.weight"], torch.arange(12.0).reshape(3, 4))
    assert vendor.same_tensors(pickled, target, TIED) == 3


def test_the_conversion_writes_the_same_bytes_every_time(
    vendor: ModuleType, pickled: Path, tmp_path: Path
) -> None:
    """What lets the lockfile pin a file the Hub does not have. The metadata
    is one key: `safetensors` writes several in a random order (measured,
    RFC-035 section 16), which is what would make two runs differ."""
    first, second = tmp_path / "first.safetensors", tmp_path / "second.safetensors"
    vendor.convert_pickle(pickled, first, TIED)
    vendor.convert_pickle(pickled, second, TIED)

    assert first.read_bytes() == second.read_bytes()
    assert metadata_of(first) == {"format": "pt"}


def test_a_tensor_with_no_name_left_to_keep_is_refused(
    vendor: ModuleType, pickled: Path, tmp_path: Path
) -> None:
    with pytest.raises(vendor.VendorError, match="exactly one must be kept"):
        vendor.convert_pickle(
            pickled, tmp_path / "out.safetensors", (*TIED, "model.shared.weight")
        )


@pytest.fixture()
def converted_lock(
    vendor: ModuleType, pickled: Path, tmp_path: Path
) -> tuple[dict[str, Any], bytes, Path]:
    """A lockfile whose translator weights are converted, and its cache."""
    made = tmp_path / "expected.safetensors"
    vendor.convert_pickle(pickled, made, TIED)
    files = {
        name: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in CONTENTS["translator"].items()
        if name != "model.safetensors"
    }
    files["model.safetensors"] = {
        "size": made.stat().st_size,
        "sha256": vendor.file_sha256(made),
    }
    revision = "0123456789abcdef0123456789abcdef01234567"
    lock: dict[str, Any] = {
        "translator": {
            "repository": "example/translator",
            "revision": revision,
            "directory": "translator-model",
            "license": "apache-2.0",
            "files": files,
            "converted": {
                "model.safetensors": {
                    "from": "pytorch_model.bin",
                    "size": pickled.stat().st_size,
                    "sha256": vendor.file_sha256(pickled),
                    "tied": list(TIED),
                }
            },
        }
    }
    snapshot = tmp_path / "hub" / "models--example--translator" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    for name, content in CONTENTS["translator"].items():
        if name != "model.safetensors":
            (snapshot / name).write_bytes(content)
    (snapshot / "pytorch_model.bin").write_bytes(pickled.read_bytes())
    return lock, (json.dumps(lock, indent=2) + "\n").encode(), tmp_path / "hub"


def test_the_tree_holds_the_converted_weights_and_no_pickle(
    vendor: ModuleType,
    converted_lock: tuple[dict[str, Any], bytes, Path],
    tmp_path: Path,
) -> None:
    lock, lock_bytes, hub = converted_lock
    target = tmp_path / "runtime" / "models"

    build(vendor, lock, lock_bytes, target, vendor.from_cache(hub))

    directory = target / "translator-model"
    assert (directory / "model.safetensors").is_file()
    assert not (directory / "pytorch_model.bin").exists()
    runtime = model_runtime.ModelRuntime(
        target,
        Lockfile.parse(lock_bytes),
        Environment.DEVELOPMENT,
        None,
        offline=lambda: True,
    )
    assert runtime.verified_directory("translator") == directory


def test_a_pickle_whose_hash_is_wrong_is_never_unpickled(
    vendor: ModuleType,
    converted_lock: tuple[dict[str, Any], bytes, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pickle's hash is checked before `torch.load` is called at all."""
    import torch

    lock, lock_bytes, hub = converted_lock
    pickle = next(hub.glob("models--example--translator/snapshots/*/pytorch_model.bin"))
    content = bytearray(pickle.read_bytes())
    content[-1] ^= 0x01
    pickle.write_bytes(bytes(content))
    unpickled: list[Path] = []
    monkeypatch.setattr(torch, "load", lambda path, **_: unpickled.append(path))

    with pytest.raises(vendor.VendorError, match="pytorch_model.bin: sha256"):
        build(vendor, lock, lock_bytes, tmp_path / "models", vendor.from_cache(hub))

    assert unpickled == []
    assert not (tmp_path / "models").exists()


def test_a_conversion_that_does_not_give_the_lockfiles_bytes_is_refused(
    vendor: ModuleType,
    converted_lock: tuple[dict[str, Any], bytes, Path],
    tmp_path: Path,
) -> None:
    lock, _, hub = converted_lock
    lock["translator"]["files"]["model.safetensors"]["sha256"] = "0" * 64
    lock_bytes = (json.dumps(lock, indent=2) + "\n").encode()

    with pytest.raises(vendor.VendorError, match="did not give the lockfile's"):
        build(vendor, lock, lock_bytes, tmp_path / "models", vendor.from_cache(hub))

    assert not (tmp_path / "models").exists()
