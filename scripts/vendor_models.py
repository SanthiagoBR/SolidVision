"""Materialize the two models from the lockfile (RFC-035 section 4.3).

The app loads CLIP and the translator from a directory, never by name, and
checks every file against `backend/app/infrastructure/ai/models.lock.json`
before it loads it (RFC-035 sections 5 and 6). This script makes that
directory, `runtime/models`, the way `vendor_postgres.py` makes
`runtime/postgres`:

    python scripts/vendor_models.py               # the Hub, at the pinned commits
    python scripts/vendor_models.py --from-cache  # the local Hugging Face cache
    python scripts/vendor_models.py --lock        # rewrite the lockfile first

**The origin of the bytes does not matter; the hash decides.** Without an
option every file is downloaded with `hf_hub_download(..., revision=<commit>)`,
at the commit the lockfile pins and never at `main`. With `--from-cache`, the
files of the pinned snapshot in the local cache are used instead: on the
machine that wrote the lockfile, that is 924 MB that do not have to be
downloaded again. Either way, a file is accepted only with the size and the
sha256 the lockfile gives it.

**One file is not the Hub's.** The translator's `main` holds its weights only
as `pytorch_model.bin`, a pickle. Section 15 of the RFC measured a
`safetensors` copy of them loading 2.2 s faster, cold, and by the criterion of
section 7.4 the conversion entered: the tree holds a `model.safetensors` made
here, and the app never unpickles anything. The lockfile names both -- the
pickle the conversion starts from, under `converted`, and the file it makes,
under `files` -- so the pickle is checked against its hash before it is
unpickled, and the result against its own after. The conversion writes the
same bytes every time (`convert_pickle`).

What it does, in order:

1. Copies every file of the lockfile into a temporary sibling of the target,
   hashing it as it goes, and refuses the first one whose size or sha256 is
   not the lockfile's; converts the one that is converted. Nothing else goes
   in: no `blobs`, no links, no `refs`, no pickle.
2. Writes `SOLIDVISION-MODELS.json`, the manifest that names the lockfile the
   tree was made from, and only then renames the directory into place. A run
   killed half way leaves no tree that looks complete.

`--lock` lists the files of each pinned snapshot in the local cache -- the
files a load asks for, which is what RFC-035 section 2.1 found the snapshot to
be -- and writes the size and sha256 of each. It then asks the Hub what the
same commit holds: a large file is checked against the sha256 of its LFS
pointer, a small one against its git blob id, so every hash of a file the Hub
has comes from two sources. The converted file's hash comes from converting,
and the conversion is checked tensor by tensor against the pickle. The license
comes from the model card.

**This script imports `huggingface_hub` directly, and never the
`app.infrastructure.ai` package.** It is the one process of the project with a
reason to reach the network, and that package turns the offline mode on the
moment it is imported (RFC-035 section 5.2). `HF_HOME` is pointed at a
temporary directory for the same reason it is in the app: nothing it writes
lands in the user's profile.

`runtime/` is outside git (`.gitignore`): 924 MB the lockfile reproduces byte
for byte, and the directory the installer will copy from.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
LOCKFILE = REPO / "backend" / "app" / "infrastructure" / "ai" / "models.lock.json"
DEFAULT_TARGET = REPO / "runtime" / "models"
MANIFEST_NAME = "SOLIDVISION-MODELS.json"
MANIFEST_FORMAT = 1
"""Both must agree with `app.infrastructure.ai.model_runtime`, which reads the
manifest; a test holds them together. Not imported from there, for the reason
the docstring gives."""

PINNED = {
    "clip": (
        "laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
        "1a25a446712ba5ee05982a381eed697ef9b435cf",
        "clip-vit-b-32-laion2b-s34b-b79k",
    ),
    "translator": (
        "Helsinki-NLP/opus-mt-ROMANCE-en",
        "e9ca9975e3972afd80732f08ce01d3a1339f47f8",
        "opus-mt-romance-en",
    ),
}
"""Repository, revision and directory of each model. Used only by `--lock`.

The revisions are the ones `refs/main` of the cache resolved on 2026-08-20,
when RFC-023 was implemented, and so the ones that produced every vector of
the development database (RFC-035 section 7.1). Changing one is a new RFC: a
migration and a reindexing plan, not an edit here (section 4.4).
"""

CONVERSIONS: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "translator": {
        "model.safetensors": (
            "pytorch_model.bin",
            ("model.decoder.embed_tokens.weight", "model.encoder.embed_tokens.weight"),
        ),
    },
}
"""Model -> {file made here: (the pickle it is made from, the tied names)}.
Used only by `--lock`, which writes it into the lockfile.

The tied names share one tensor with `model.shared.weight` in the pickle, and
`safetensors` stores a tensor once: they are dropped, and `transformers` ties
them again on load, from the model's configuration. The same choice the Hub's
own conversion bot made for `refs/pr/6` (RFC-035 section 16).
"""

CHUNK = 1 << 20

Fetch = Callable[[str, str, str], Path]
"""(repository, revision, filename) -> a local file holding those bytes."""


class VendorError(Exception):
    """A step failed. The message says which; nothing half-built is left in place."""


def mb(size: int) -> float:
    return size / (1024 * 1024)


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def git_blob_id(path: Path) -> str:
    """What git -- and the Hub, for a file outside LFS -- calls this content."""
    hasher = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def lockfile_digest(content: bytes) -> str:
    """The lockfile's sha256 with line endings normalized.

    Git checks the file out with CRLF or LF depending on `core.autocrlf`, and
    the manifest must name the same lockfile on every machine. The app
    computes the same digest the same way.
    """
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def hub_cache() -> Path:
    """The local Hugging Face cache, where `huggingface_hub` would put it.

    Read from the environment by hand, before anything imports the library:
    `main()` points `HF_HOME` elsewhere for the downloads.
    """
    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(name):
            return Path(os.path.expandvars(os.path.expanduser(os.environ[name])))
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join("~", ".cache"),
        "huggingface",
    )
    return Path(os.path.expandvars(os.path.expanduser(home))) / "hub"


def snapshot_directory(cache: Path, repository: str, revision: str) -> Path:
    return cache / ("models--" + repository.replace("/", "--")) / "snapshots" / revision


def verify(path: Path, size: int, sha256: str) -> None:
    """Refuse a file that is not the lockfile's, naming what is wrong."""
    found_size = path.stat().st_size
    if found_size != size:
        raise VendorError(f"{path}: {found_size} bytes, the lockfile says {size}")
    found = file_sha256(path)
    if found != sha256:
        raise VendorError(
            f"{path}: sha256 {found[:12]}..., the lockfile says {sha256[:12]}..."
        )


# --------------------------------------------------------------------------
# The conversion
# --------------------------------------------------------------------------


def convert_pickle(source: Path, target: Path, tied: Sequence[str]) -> None:
    """The tensors of a pickle as a `safetensors` file, the same bytes every time.

    **Only ever called on a source whose sha256 was checked against the
    lockfile.** `torch.load` unpickles, and `weights_only` is a restriction of
    today's `torch`, not a promise of every version's.

    Names that share one tensor with another and are listed in `tied` are
    dropped; exactly one name of each such group must remain. The metadata is
    one key, `{"format": "pt"}`, and that is what makes the bytes repeatable:
    `safetensors` 0.8 writes a metadata of several keys in a random order, so
    the aliases the Hub's bot records there would change the header from one
    run to the next (measured, RFC-035 section 16). `transformers` ties the
    dropped names from the model's configuration, not from the metadata.
    """
    import torch
    from safetensors.torch import save_file

    state = torch.load(source, map_location="cpu", weights_only=True)
    groups: dict[int, list[str]] = {}
    for name, tensor in state.items():
        groups.setdefault(tensor.untyped_storage().data_ptr(), []).append(name)
    for names in groups.values():
        if len(names) < 2:
            continue
        kept = [name for name in names if name not in tied]
        if len(kept) != 1:
            raise VendorError(
                f"{source.name}: {sorted(names)} are one tensor, and the lockfile "
                f"ties all of them but {kept}; exactly one must be kept"
            )
        for name in names:
            if name != kept[0]:
                del state[name]
    tensors = {name: tensor.contiguous() for name, tensor in state.items()}
    save_file(tensors, str(target), metadata={"format": "pt"})


def same_tensors(source: Path, converted: Path, tied: Sequence[str]) -> int:
    """Check the conversion tensor by tensor; return how many there were."""
    import torch
    from safetensors.torch import load_file

    pickled = torch.load(source, map_location="cpu", weights_only=True)
    written = load_file(str(converted))
    expected = {name: tensor for name, tensor in pickled.items() if name not in tied}
    if sorted(expected) != sorted(written):
        raise VendorError(f"{converted.name}: other tensor names than {source.name}")
    for name, tensor in expected.items():
        if not torch.equal(tensor, written[name]):
            raise VendorError(f"{converted.name}: {name} differs from {source.name}")
    return len(written)


# --------------------------------------------------------------------------
# The lockfile
# --------------------------------------------------------------------------


def read_lock(lockfile: Path) -> dict[str, Any]:
    """The lockfile, checked for the shape the app and this script rely on."""
    try:
        lock = json.loads(lockfile.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VendorError(
            f"{lockfile} is missing: run with --lock to create it"
        ) from None
    except ValueError as error:
        raise VendorError(f"{lockfile} is not JSON: {error}") from None
    if not isinstance(lock, dict) or not lock:
        raise VendorError(f"{lockfile} names no model")
    for key, model in lock.items():
        files = model.get("files") if isinstance(model, dict) else None
        if not isinstance(files, dict) or not files:
            raise VendorError(f"{lockfile}: {key} lists no files")
        for field in ("repository", "revision", "directory"):
            if not isinstance(model.get(field), str) or not model[field]:
                raise VendorError(f"{lockfile}: {key} has no {field}")
        converted = model.get("converted", {})
        entries = [*files.items(), *converted.items()]
        for name, entry in entries:
            if (
                not isinstance(entry.get("size"), int)
                or len(entry.get("sha256", "")) != 64
            ):
                raise VendorError(f"{lockfile}: {key}/{name} has no size or sha256")
        for name, entry in converted.items():
            if name not in files or not entry.get("from"):
                raise VendorError(
                    f"{lockfile}: {key}/{name} is converted, and the lockfile "
                    "does not say from what, or does not list the result"
                )
    return lock


def checked_on_the_hub(path: Path, sibling: Any, repository: str) -> str:
    """Compare one local file with what the Hub declares for it."""
    size = path.stat().st_size
    sha256 = file_sha256(path)
    if sibling is None:
        raise VendorError(f"{path.name} is not in {repository}")
    if sibling.size != size:
        raise VendorError(
            f"{repository}/{path.name}: {size} bytes here, {sibling.size} on the Hub"
        )
    if sibling.lfs is not None:
        if sibling.lfs["sha256"] != sha256:
            raise VendorError(
                f"{repository}/{path.name}: sha256 {sha256}, the LFS pointer "
                f"says {sibling.lfs['sha256']}"
            )
        return "LFS sha256"
    blob = git_blob_id(path)
    if sibling.blob_id != blob:
        raise VendorError(
            f"{repository}/{path.name}: git blob {blob}, the Hub says {sibling.blob_id}"
        )
    return "git blob id"


def write_lock(cache: Path, lockfile: Path, hub: Any, work: Path) -> dict[str, Any]:
    """Hash every file of each pinned snapshot, check it on the Hub, convert
    what is converted, and write the lockfile."""
    lock: dict[str, Any] = {}
    for key, (repository, revision, directory) in PINNED.items():
        snapshot = snapshot_directory(cache, repository, revision)
        if not snapshot.is_dir():
            raise VendorError(
                f"--lock reads the files of {repository}@{revision[:12]} from "
                f"the local cache, and {snapshot} does not exist. Load the "
                "model by name once, at that revision, to create it."
            )
        info = hub.model_info(repository, revision=revision, files_metadata=True)
        if info.sha != revision:
            raise VendorError(f"the Hub resolved {repository}@{revision} to {info.sha}")
        siblings = {sibling.rfilename: sibling for sibling in info.siblings or ()}
        conversions = CONVERSIONS.get(key, {})
        sources = {source: made for made, (source, _) in conversions.items()}
        files: dict[str, dict[str, Any]] = {}
        converted: dict[str, dict[str, Any]] = {}
        for path in sorted(snapshot.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                continue
            how = checked_on_the_hub(path, siblings.get(path.name), repository)
            entry = {"size": path.stat().st_size, "sha256": file_sha256(path)}
            print(f"  {key}/{path.name}: {entry['size']} bytes, the Hub's {how} OK")
            if path.name not in sources:
                files[path.name] = entry
                continue
            made = sources[path.name]
            tied = conversions[made][1]
            target = work / key / made
            target.parent.mkdir(parents=True, exist_ok=True)
            convert_pickle(path, target, tied)
            count = same_tensors(path, target, tied)
            files[made] = {"size": target.stat().st_size, "sha256": file_sha256(target)}
            converted[made] = {"from": path.name, **entry, "tied": list(tied)}
            print(
                f"  {key}/{made}: converted from {path.name}, {count} tensors equal "
                f"to the pickle's, {files[made]['size']} bytes"
            )
        missing = sorted(set(sources) - {entry["from"] for entry in converted.values()})
        if missing:
            raise VendorError(f"{key}: {missing} is not in {snapshot}")
        card = info.card_data
        license_ = card.get("license") if card is not None else None
        lock[key] = {
            "repository": repository,
            "revision": revision,
            "directory": directory,
            "license": str(license_) if license_ else "unknown",
            "files": dict(sorted(files.items())),
            **({"converted": converted} if converted else {}),
        }
    lockfile.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    total = sum(
        entry["size"] for model in lock.values() for entry in model["files"].values()
    )
    print(f"{lockfile.relative_to(REPO)}: {len(lock)} models, {total} bytes")
    return lock


# --------------------------------------------------------------------------
# Materializing
# --------------------------------------------------------------------------


def from_cache(cache: Path) -> Fetch:
    """Take each file from the pinned snapshot of the local cache."""

    def fetch(repository: str, revision: str, filename: str) -> Path:
        path = snapshot_directory(cache, repository, revision) / filename
        if not path.is_file():
            raise VendorError(
                f"{filename} of {repository}@{revision[:12]} is not in the cache "
                f"({path}). Run without --from-cache to download it."
            )
        return path

    return fetch


def from_hub(download_cache: Path) -> Fetch:
    """Download each file at the pinned revision, into a temporary cache."""
    from huggingface_hub import hf_hub_download

    def fetch(repository: str, revision: str, filename: str) -> Path:
        return Path(
            hf_hub_download(
                repository, filename, revision=revision, cache_dir=download_cache
            )
        )

    return fetch


def copy_verified(source: Path, destination: Path, size: int, sha256: str) -> None:
    """Copy one file, hashing it on the way, and keep it only if it is the lock's."""
    hasher = hashlib.sha256()
    written = 0
    with source.open("rb") as reading, destination.open("xb") as writing:
        for chunk in iter(lambda: reading.read(CHUNK), b""):
            hasher.update(chunk)
            writing.write(chunk)
            written += len(chunk)
    found = hasher.hexdigest()
    if written != size or found != sha256:
        destination.unlink()
        what = (
            f"{written} bytes, the lockfile says {size}"
            if written != size
            else f"sha256 {found[:12]}..., the lockfile says {sha256[:12]}..."
        )
        raise VendorError(f"{source}: {what}. It was not copied.")


def build_tree(
    lock: Mapping[str, Any], staging: Path, fetch: Fetch
) -> dict[str, dict[str, Any]]:
    """Fill `staging` with every file of the lock; return what each model took."""
    built: dict[str, dict[str, Any]] = {}
    for key, model in lock.items():
        directory = staging / model["directory"]
        directory.mkdir(parents=True)
        converted = model.get("converted", {})
        for name, entry in sorted(model["files"].items()):
            if name not in converted:
                source = fetch(model["repository"], model["revision"], name)
                copy_verified(source, directory / name, entry["size"], entry["sha256"])
                continue
            recipe = converted[name]
            source = fetch(model["repository"], model["revision"], recipe["from"])
            # The pickle's hash first: nothing is unpickled that is not it.
            verify(source, recipe["size"], recipe["sha256"])
            convert_pickle(source, directory / name, recipe["tied"])
            try:
                verify(directory / name, entry["size"], entry["sha256"])
            except VendorError as error:
                raise VendorError(
                    f"the conversion of {recipe['from']} did not give the "
                    f"lockfile's {name}: {error}"
                ) from None
        total = sum(entry["size"] for entry in model["files"].values())
        built[key] = {
            "repository": model["repository"],
            "revision": model["revision"],
            "directory": model["directory"],
            "files": len(model["files"]),
            "converted": sorted(converted),
            "bytes": total,
        }
        made = f", {', '.join(sorted(converted))} converted" if converted else ""
        print(
            f"  {key}: {len(model['files'])} files{made}, {mb(total):.0f} MB, "
            "sha256 OK"
        )
    return built


def materialize(
    lock: Mapping[str, Any],
    lock_sha256: str,
    target: Path,
    fetch: Fetch,
    source: str,
) -> dict[str, Any]:
    """Build the tree beside `target` and rename it into place, or leave none.

    The previous tree, if there is one, is kept until the new one is complete,
    and is what `target` still holds when anything goes wrong.
    """
    staging = target.with_name(target.name + ".vendor-tmp")
    shutil.rmtree(staging, ignore_errors=True)
    if staging.exists():
        raise VendorError(f"{staging} is left over and could not be removed")
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        models = build_tree(lock, staging, fetch)
        manifest = {
            "format": MANIFEST_FORMAT,
            "lockfile": LOCKFILE.name,
            "lockfile_sha256": lock_sha256,
            "source": source,
            "materialized_at": datetime.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds"),
            "bytes": sum(model["bytes"] for model in models.values()),
            "models": models,
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    swap_into_place(staging, target)
    return manifest


def swap_into_place(staging: Path, target: Path) -> None:
    """Rename the new tree to `target`, retiring the old one first.

    A process that has a model loaded keeps its weights mapped, and Windows
    refuses to rename a directory with a mapped file in it. The new tree is
    then left where it is, complete, and the message says what to stop.
    """
    retired = target.with_name(target.name + ".vendor-old")
    shutil.rmtree(retired, ignore_errors=True)
    try:
        if target.exists():
            target.rename(retired)
    except OSError as error:
        raise VendorError(
            f"{target} could not be replaced ({error.strerror}): a process has a "
            "model loaded from it. Stop the API and the job runner and run this "
            f"again. Nothing was changed; the new tree is waiting in {staging}."
        ) from None
    try:
        staging.rename(target)
    except OSError:
        if retired.exists():
            retired.rename(target)
        raise
    shutil.rmtree(retired, ignore_errors=True)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help=f"where the tree goes (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="take the files from the local Hugging Face cache instead of the Hub",
    )
    parser.add_argument(
        "--lock",
        action="store_true",
        help="rewrite the lockfile from the pinned snapshots first",
    )
    args = parser.parse_args()

    target: Path = args.target.resolve()
    cache = hub_cache()
    work = Path(tempfile.mkdtemp(prefix="solidvision-models-"))
    # Before the first import of huggingface_hub, which reads both once
    # (RFC-035 section 2.3): what the library writes goes to `work`.
    os.environ["HF_HOME"] = str(work / "hf-home")
    started = time.perf_counter()
    try:
        if args.lock:
            from huggingface_hub import HfApi

            write_lock(cache, LOCKFILE, HfApi(), work / "lock")
        lock = read_lock(LOCKFILE)
        if args.from_cache:
            fetch, source = from_cache(cache), "cache"
            print(f"source: the local cache, {cache}")
        else:
            offline = os.environ.get("HF_HUB_OFFLINE", "").upper()
            if offline in ("1", "ON", "YES", "TRUE"):
                raise VendorError(
                    "HF_HUB_OFFLINE is set in this shell, so nothing can be "
                    "downloaded. Unset it, or use --from-cache."
                )
            fetch, source = from_hub(work / "download"), "hub"
            print("source: the Hugging Face Hub, at the pinned revisions")
        digest = lockfile_digest(LOCKFILE.read_bytes())
        manifest = materialize(lock, digest, target, fetch, source)
        elapsed = time.perf_counter() - started
        print(f"models: {target}")
        print(
            f"  {mb(manifest['bytes']):.0f} MB ({manifest['bytes']} bytes) in "
            f"{elapsed:.1f} s, lockfile {digest[:12]}"
        )
        return 0
    except (VendorError, OSError) as error:
        print(f"FAILED: {error}")
        return 1
    except Exception as error:  # noqa: BLE001 -- a download error, said in one line
        print(f"FAILED: {type(error).__name__}: {error}")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
