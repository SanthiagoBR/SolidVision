"""The two models as a runtime: found by a setting, checked against code (RFC-035).

The lockfile is code. `models.lock.json`, beside this module, names the
repository, the revision and every file of CLIP and of the translator, with
the size and the sha256 of each (section 4.1). Changing a model is a commit to
it, reviewed, and not a line in a configuration file.

`scripts/vendor_models.py` materializes the lockfile into a tree, by default
`runtime\\models` beside the code (section 4.2)::

    <runtime>\\
        SOLIDVISION-MODELS.json              the lockfile that made the tree
        clip-vit-b-32-laion2b-s34b-b79k\\     the 8 files of the CLIP snapshot
        opus-mt-romance-en\\                  the 7 files of the translator's

and this module is the other half: it checks the tree against the lockfile of
the code that is running, and only then hands a directory to an adapter.

What is checked, and when (section 6.1):

    on the start of a process      the manifest; every file of both models
    that will load models          present, with its size (`check_presence`)
    before each load of a model    the same for that model, and the sha256 of
                                   each of its files (`verified_directory`)

**The sha256 before the load, never after.** It is what gives the right
message: a truncated file loaded first reaches the user as a traceback from
torch (section 6.2). No pickle is left to guard: the translator's
`pytorch_model.bin` is converted to `safetensors` by `vendor_models.py`, which
checks the pickle's hash before it unpickles it (the second branch of section
7.4), and the tree holds no pickle at all.

**The second branch of section 6.3.** Hashing the weights costs several times
what loading them does (RFC-035 section 15: CLIP's 605 MB hash in 2.4 s warm
and 4.7 s cold, and load in 0.6 s and 2.0 s), so a file whose path, size,
modification time and NTFS file id are those of a file already hashed against
this lockfile is not hashed again. That record, `models-verified.json` in the
user's cache folder, is written after every successful hash. Presence and size
are still checked on every load. What the branch gives up, declared: a
corruption of the disk that changes neither the size nor the dates is caught
only by the next full check.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from app.infrastructure.config.layout import Environment, Layout, layout
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

LOCKFILE_NAME = "models.lock.json"
MANIFEST_NAME = "SOLIDVISION-MODELS.json"
MANIFEST_FORMAT = 1
RECORD_NAME = "models-verified.json"
RECORD_FORMAT = 1

CLIP = "clip"
TRANSLATOR = "translator"
"""The keys of the lockfile: what the code asks for by name."""

REMEDIES = {
    Environment.DEVELOPMENT: (
        "Materialize the models with `python scripts/vendor_models.py`."
    ),
    Environment.INSTALLED: "Reinstall SolidVision.",
}
"""What to do, at the end of every message: what can be done is per mode."""

ModelDirectory = Callable[[], Path]
"""What an adapter receives instead of a name: called on its first load, it
returns the model's directory, checked, or raises `ModelUnavailableError`.

A callable rather than a path, so that constructing an adapter stays free
(RFC-023 section 9) and the check happens on the load, not before.
"""


class ModelUnavailableError(Exception):
    """A model cannot be loaded, and the message is one line saying why.

    The model, the file, the problem and what to do (section 6.4), and never
    the content of any file: names, sizes and the prefix of a hash. Who shows
    it decides the rest (section 6.5): the job runner and the CLI print it and
    exit with 1; the API answers 503 with it on the routes that need a model.
    Restarting fixes none of the cases.
    """


@dataclass(frozen=True)
class LockedFile:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class LockedModel:
    key: str
    repository: str
    revision: str
    directory: str
    license: str
    files: tuple[LockedFile, ...]


@dataclass(frozen=True)
class Lockfile:
    """The lockfile, parsed, and the digest that names it in a manifest."""

    models: Mapping[str, LockedModel]
    sha256: str

    @classmethod
    def parse(cls, content: bytes) -> Lockfile:
        """The lockfile from its bytes, or `ValueError` saying what is wrong."""
        raw = json.loads(content.decode("utf-8"))
        if not isinstance(raw, dict) or not raw:
            raise ValueError("the lockfile names no model")
        models = {}
        for key, entry in raw.items():
            files = entry.get("files") if isinstance(entry, dict) else None
            if not isinstance(files, dict) or not files:
                raise ValueError(f"{key} lists no files")
            models[key] = LockedModel(
                key=key,
                repository=str(entry["repository"]),
                revision=str(entry["revision"]),
                directory=str(entry["directory"]),
                license=str(entry.get("license", "")),
                files=tuple(
                    LockedFile(name, int(spec["size"]), str(spec["sha256"]))
                    for name, spec in sorted(files.items())
                ),
            )
        return cls(models=models, sha256=lockfile_digest(content))

    def model(self, key: str) -> LockedModel:
        try:
            return self.models[key]
        except KeyError:
            raise ModelUnavailableError(
                f"The lockfile names no model {key!r}; this is a defect of this "
                "version of SolidVision."
            ) from None


def lockfile_digest(content: bytes) -> str:
    """The lockfile's sha256, line endings normalized, as the manifest names it.

    Git checks the file out with CRLF or LF depending on `core.autocrlf`; the
    digest must be the same on every machine. `vendor_models.py` computes it
    the same way.
    """
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


@functools.cache
def read_lockfile() -> Lockfile:
    """The lockfile of the running code, read as data of this package.

    Through `importlib.resources`, and not a path climbed from a file: the
    lockfile travels with the code, and an installed app has no `scripts\\`.
    """
    content = (resources.files("app.infrastructure.ai") / LOCKFILE_NAME).read_bytes()
    try:
        return Lockfile.parse(content)
    except (ValueError, KeyError, TypeError) as error:
        raise ModelUnavailableError(
            f"The lockfile of this version, {LOCKFILE_NAME}, could not be read "
            f"({error!r}); this is a defect of this version of SolidVision."
        ) from None


@functools.cache
def default_runtime() -> ModelRuntime:
    """The tree `settings.model_runtime_directory` names, for this process.

    Checked against the running code's lockfile, never trusted: pointing the
    setting at another tree can only point it at an identical copy (RFC-035
    section 4.5). Building it reads the small lockfile and nothing else.

    One per process, so that a refusal it remembers -- a file whose hash did
    not match -- holds for every caller. Here, and not in the composition
    root, because a process that only checks presence at its start -- the
    job runner, the CLI -- must be able to refuse without importing `torch`
    first, which takes longer than everything else it does before that.
    """
    from app.infrastructure.config.settings import settings

    return ModelRuntime.of(settings.model_runtime_directory, layout)


def huggingface_is_offline() -> bool:
    """Whether `huggingface_hub` would refuse every request.

    Imported here, on the first load, and not at the top: the package
    `__init__` has run by then, so a first import here sees the variable.
    """
    import huggingface_hub

    return bool(huggingface_hub.is_offline_mode())


def load_failed(directory: Path) -> ModelUnavailableError:
    """Row six of section 6.4: every file checked out, and the load failed anyway.

    Called inside an `except` block: the traceback goes to the log, and the
    line that is returned does not repeat it.
    """
    logger.exception("Loading the model in %s failed", directory)
    return ModelUnavailableError(
        f"The model in {directory} could not be loaded; the traceback is in the log."
    )


class ModelRuntime:
    """One tree of models, refused unless it is the one the lockfile describes."""

    def __init__(
        self,
        root: Path,
        lockfile: Lockfile,
        environment: Environment,
        record_file: Path | None,
        offline: Callable[[], bool] = huggingface_is_offline,
    ) -> None:
        if not root.is_absolute():
            raise ValueError(f"The model runtime must be absolute, not {root}.")
        self._root = root
        self._lockfile = lockfile
        self._remedy = REMEDIES[environment]
        self._record_file = record_file
        self._offline = offline
        self._lock = threading.Lock()
        self._refused: dict[tuple[str, str], str] = {}
        """Files whose hash was already refused, by path and identity.

        So that a corrupt tree costs one hash, and not one per search request
        answered with 503 -- while a file that was replaced since, and so has
        another identity, is hashed again: fixing the tree needs no restart.
        """

    @classmethod
    def of(cls, root: Path, where: Layout) -> ModelRuntime:
        """The tree at `root`, checked against the running code's lockfile."""
        return cls(
            root,
            read_lockfile(),
            environment=where.environment,
            record_file=where.cache_directory / RECORD_NAME,
        )

    def loader(self, key: str) -> ModelDirectory:
        """What an adapter is given: the checked directory, on its first load."""
        return functools.partial(self.verified_directory, key)

    def check_presence(self) -> None:
        """The check of a process's start: cheap, and of both models.

        The manifest, and every file of every model present with its size: one
        small JSON file and fifteen `stat`s. It finds a broken installation
        when the process starts, and not on the first search in Portuguese.
        """
        self._check_manifest()
        for model in self._lockfile.models.values():
            self._stat_files(model)

    def verified_directory(self, key: str) -> Path:
        """The directory of one model, after its files were checked in full.

        One thread at a time: two first requests that both find the adapter
        unloaded hash once, and the second finds the record the first wrote.
        """
        model = self._lockfile.model(key)
        with self._lock:
            self._require_offline(model)
            self._check_manifest()
            stats = self._stat_files(model)
            self._verify_contents(model, stats)
        return self._root / model.directory

    # ------------------------------------------------------------------

    def _require_offline(self, model: LockedModel) -> None:
        """Section 5.2: the variable only works if it came before the import.

        Something that imported `huggingface_hub` before this package -- a
        `sitecustomize`, a tool, a test -- left it reading the environment as
        it was then. Loading anyway would load with the network on.
        """
        if not self._offline():
            raise ModelUnavailableError(
                f"{model.repository} was not loaded: huggingface_hub was imported "
                "before app.infrastructure.ai turned its offline mode on, so it "
                "would reach the network. Import app.infrastructure.ai before "
                "anything that imports transformers or huggingface_hub."
            )

    def _check_manifest(self) -> None:
        path = self._root / MANIFEST_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ModelUnavailableError(
                f"The models are not in {self._root}. {self._remedy}"
            ) from None
        except (OSError, ValueError) as error:
            raise ModelUnavailableError(
                f"{path} could not be read ({error}). {self._remedy}"
            ) from None
        if (
            not isinstance(raw, dict)
            or raw.get("format") != MANIFEST_FORMAT
            or not isinstance(raw.get("lockfile_sha256"), str)
        ):
            raise ModelUnavailableError(
                f"{path} is not a manifest this version of SolidVision can read. "
                f"{self._remedy}"
            )
        found = raw["lockfile_sha256"]
        if found != self._lockfile.sha256:
            raise ModelUnavailableError(
                f"The models in {self._root} are from another version of "
                f"SolidVision: they were materialized from lockfile {found[:12]}, "
                f"and this version's is {self._lockfile.sha256[:12]}. {self._remedy}"
            )

    def _stat_files(self, model: LockedModel) -> dict[str, os.stat_result]:
        directory = self._root / model.directory
        stats = {}
        for locked in model.files:
            path = directory / locked.name
            try:
                found = path.stat()
            except FileNotFoundError:
                found = None
            except OSError as error:
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model, in "
                    f"{directory}, cannot be read ({error.strerror}). An "
                    f"antivirus may be holding it. {self._remedy}"
                ) from None
            if found is None or not stat.S_ISREG(found.st_mode):
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model is missing "
                    f"from {directory}. An antivirus may have quarantined it. "
                    f"{self._remedy}"
                )
            if found.st_size != locked.size:
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model has "
                    f"{found.st_size} bytes, and this version expects "
                    f"{locked.size}: the copy in {directory} is incomplete. "
                    f"{self._remedy}"
                )
            stats[locked.name] = found
        return stats

    def _verify_contents(
        self, model: LockedModel, stats: Mapping[str, os.stat_result]
    ) -> None:
        directory = self._root / model.directory
        vouched = self._read_record()
        pending = []
        for locked in model.files:
            path = str(directory / locked.name)
            identity = _identity(stats[locked.name], locked.sha256)
            if vouched.get(path) == identity:
                continue
            refusal = self._refused.get((path, json.dumps(identity, sort_keys=True)))
            if refusal is not None:
                raise ModelUnavailableError(refusal)
            pending.append((locked, path, identity))
        if not pending:
            return
        for locked, path, identity in pending:
            try:
                with open(path, "rb") as handle:
                    found = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError as error:
                raise ModelUnavailableError(
                    f"{locked.name} of the {model.repository} model, in "
                    f"{directory}, cannot be read ({error.strerror}). An "
                    f"antivirus may be holding it. {self._remedy}"
                ) from None
            if found != locked.sha256:
                refusal = (
                    f"{locked.name} of the {model.repository} model is not the "
                    f"file this version expects (sha256 {found[:12]}..., expected "
                    f"{locked.sha256[:12]}...): its content was altered or "
                    f"corrupted. {self._remedy}"
                )
                self._refused[(path, json.dumps(identity, sort_keys=True))] = refusal
                raise ModelUnavailableError(refusal)
            vouched[path] = identity
        logger.info(
            "Checked the sha256 of %d file(s) of %s against the lockfile",
            len(pending),
            model.repository,
        )
        self._write_record(vouched)

    def _read_record(self) -> dict[str, Any]:
        """The files already hashed against this lockfile, by absolute path.

        A record that cannot be read, or that was written for another
        lockfile, vouches for nothing: the cost is one more hash.
        """
        if self._record_file is None:
            return {}
        try:
            raw = json.loads(self._record_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning(
                "The verification record %s could not be read (%s); the models "
                "are hashed again",
                self._record_file,
                error,
            )
            return {}
        if (
            not isinstance(raw, dict)
            or raw.get("format") != RECORD_FORMAT
            or raw.get("lockfile_sha256") != self._lockfile.sha256
            or not isinstance(raw.get("files"), dict)
        ):
            return {}
        files: dict[str, Any] = raw["files"]
        return files

    def _write_record(self, files: Mapping[str, Any]) -> None:
        """Replace the record in one rename, so no reader sees half of one.

        Two processes writing at once lose one of the two updates, which
        costs that process's files one more hash, and nothing else. A record
        that cannot be written is logged and otherwise ignored: the models are
        right, and the next load hashes them again.
        """
        if self._record_file is None:
            return
        record = {
            "format": RECORD_FORMAT,
            "lockfile_sha256": self._lockfile.sha256,
            "files": dict(sorted(files.items())),
        }
        temporary = self._record_file.with_name(
            f"{self._record_file.name}.{os.getpid()}.tmp"
        )
        try:
            self._record_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self._record_file)
        except OSError as error:
            logger.warning(
                "The verification record %s could not be written (%s); the next "
                "load hashes the models again",
                self._record_file,
                error,
            )


def _identity(found: os.stat_result, sha256: str) -> dict[str, Any]:
    """What must not have changed for an earlier hash to still vouch for a file.

    The size, the modification time and the file id -- on Windows `st_ino` is
    the NTFS file id, and `st_dev` the volume's serial number -- and the
    sha256 the file was checked against. A file replaced by a copy has a new
    id; a file rewritten in place has a new modification time.
    """
    return {
        "size": found.st_size,
        "mtime_ns": found.st_mtime_ns,
        "file_id": f"{found.st_dev}:{found.st_ino}",
        "sha256": sha256,
    }
