"""The tree of models, checked against the lockfile before anything loads (RFC-035).

Section 6.4 promises one line per way a tree can be wrong, and section 6.2
that the check comes before the load. Both are tested over trees in
miniature (`model_trees.py`), with lockfiles of their own, so nothing here
needs `runtime\\models` or reads a real weight.

The second branch of section 6.3 is here too: the measurement found the
sha256 of the weights several times as costly as loading them, so a file
already hashed is vouched for by a record of its identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest
import torch
from tests.infrastructure.ai.model_trees import (
    CONTENTS,
    SECRET,
    Tree,
    flip_one_byte,
    make_tree,
    write_manifest,
)

from app.infrastructure.ai import model_runtime
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.ai.model_runtime import (
    CLIP,
    MANIFEST_NAME,
    RECORD_NAME,
    TRANSLATOR,
    Lockfile,
    ModelRuntime,
    ModelUnavailableError,
    lockfile_digest,
    read_lockfile,
)
from app.infrastructure.ai.query_translator import (
    MarianQueryTranslator,
    QueryTranslator,
)
from app.infrastructure.config.layout import Environment, layout_at

DEVELOPMENT_REMEDY = "Materialize the models with `python scripts/vendor_models.py`."
INSTALLED_REMEDY = "Reinstall SolidVision."


@pytest.fixture()
def tree(tmp_path: Path) -> Tree:
    return make_tree(tmp_path / "runtime" / "models")


@pytest.fixture()
def record(tmp_path: Path) -> Path:
    return tmp_path / "data" / "cache" / RECORD_NAME


@pytest.fixture()
def hashed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every file the runtime hashes, by name, in order."""
    names: list[str] = []
    original = hashlib.file_digest

    def counting(handle: Any, digest: Any) -> Any:
        names.append(Path(handle.name).name)
        return original(handle, digest)

    monkeypatch.setattr(hashlib, "file_digest", counting)
    return names


def refusal(runtime: ModelRuntime, key: str = CLIP) -> str:
    with pytest.raises(ModelUnavailableError) as raised:
        runtime.verified_directory(key)
    return str(raised.value)


# --------------------------------------------------------------------------
# A complete tree
# --------------------------------------------------------------------------


def test_a_complete_tree_is_accepted_and_each_model_is_its_directory(
    tree: Tree,
) -> None:
    runtime = tree.runtime()

    runtime.check_presence()

    assert runtime.verified_directory(CLIP) == tree.directory(CLIP)
    assert runtime.verified_directory(TRANSLATOR) == tree.directory(TRANSLATOR)


def test_constructing_a_runtime_touches_nothing(tree: Tree, tmp_path: Path) -> None:
    """`default_runtime()` is built at the composition of every process."""
    nowhere = tmp_path / "nowhere"
    record = tmp_path / "record.json"

    ModelRuntime(nowhere, tree.lockfile, Environment.DEVELOPMENT, record)

    assert not nowhere.exists()
    assert not record.exists()


def test_a_relative_tree_is_refused(tree: Tree) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ModelRuntime(
            Path("runtime", "models"), tree.lockfile, Environment.INSTALLED, None
        )


def test_a_file_the_lockfile_does_not_name_is_ignored(tree: Tree) -> None:
    """A `desktop.ini` left by Explorer must not take search down (6.1)."""
    (tree.root / "desktop.ini").write_text("[.ShellClassInfo]\n", encoding="utf-8")
    (tree.directory(CLIP) / "desktop.ini").write_text("[x]\n", encoding="utf-8")
    (tree.directory(CLIP) / "open_clip_model.safetensors").write_bytes(b"other")

    runtime = tree.runtime()
    runtime.check_presence()
    assert runtime.verified_directory(CLIP) == tree.directory(CLIP)


# --------------------------------------------------------------------------
# Section 6.4: one line per way to be wrong
# --------------------------------------------------------------------------


def test_no_tree_says_where_it_was_looked_for_and_what_to_do(
    tree: Tree, tmp_path: Path
) -> None:
    root = tmp_path / "nowhere" / "models"
    runtime = ModelRuntime(root, tree.lockfile, Environment.DEVELOPMENT, None)

    with pytest.raises(ModelUnavailableError) as raised:
        runtime.check_presence()

    assert str(raised.value) == f"The models are not in {root}. {DEVELOPMENT_REMEDY}"


def test_installed_what_to_do_is_to_reinstall(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "models")
    shutil.rmtree(tree.root)

    message = refusal(tree.runtime(Environment.INSTALLED))

    assert message.endswith(INSTALLED_REMEDY)
    assert "scripts" not in message


def test_a_directory_without_a_manifest_is_not_a_tree(tree: Tree) -> None:
    (tree.root / MANIFEST_NAME).unlink()

    assert refusal(tree.runtime()).startswith(f"The models are not in {tree.root}.")


def test_a_tree_of_another_lockfile_says_it_is_another_version(tree: Tree) -> None:
    """Section 4.3: one line, instead of fifteen files that differ."""
    write_manifest(tree.root, "f" * 64)

    message = refusal(tree.runtime())

    assert "from another version of SolidVision" in message
    assert "lockfile ffffffffffff" in message
    assert tree.lockfile.sha256[:12] in message
    assert message.endswith(DEVELOPMENT_REMEDY)


@pytest.mark.parametrize(
    "manifest", ["not json", "[]", '{"format": 2, "lockfile_sha256": "x"}', "{}"]
)
def test_a_manifest_this_version_cannot_read_is_refused(
    tree: Tree, manifest: str
) -> None:
    (tree.root / MANIFEST_NAME).write_text(manifest, encoding="utf-8")

    message = refusal(tree.runtime())

    assert str(tree.root / MANIFEST_NAME) in message
    assert message.endswith(DEVELOPMENT_REMEDY)


@pytest.mark.parametrize(
    ("key", "name"), [(CLIP, "model.safetensors"), (TRANSLATOR, "model.safetensors")]
)
def test_a_missing_file_is_named_with_the_antivirus_as_the_likely_cause(
    tree: Tree, key: str, name: str
) -> None:
    tree.file(key, name).unlink()

    with pytest.raises(ModelUnavailableError) as raised:
        tree.runtime().check_presence()

    assert str(raised.value) == (
        f"{name} of the example/{key} model is missing from {tree.directory(key)}. "
        f"An antivirus may have quarantined it. {DEVELOPMENT_REMEDY}"
    )


def test_a_folder_where_a_file_should_be_is_a_missing_file(tree: Tree) -> None:
    path = tree.file(CLIP, "config.json")
    path.unlink()
    path.mkdir()

    assert "config.json of the example/clip model is missing" in refusal(tree.runtime())


def test_a_truncated_file_says_the_copy_is_incomplete(tree: Tree) -> None:
    path = tree.file(CLIP, "model.safetensors")
    expected = path.stat().st_size
    path.write_bytes(path.read_bytes()[:10])

    message = refusal(tree.runtime())

    assert (
        f"model.safetensors of the example/clip model has 10 bytes, and this "
        f"version expects {expected}: the copy in {tree.directory(CLIP)} is "
        "incomplete."
    ) in message


@pytest.mark.parametrize(
    ("key", "name"), [(CLIP, "model.safetensors"), (TRANSLATOR, "model.safetensors")]
)
def test_a_changed_byte_is_found_by_the_hash_and_only_by_it(
    tree: Tree, key: str, name: str
) -> None:
    """The size is right, so the check of a process's start lets it through;
    the load's does not."""
    flip_one_byte(tree.file(key, name))
    runtime = tree.runtime()

    runtime.check_presence()
    message = refusal(runtime, key)

    assert message.startswith(
        f"{name} of the example/{key} model is not the file this version expects"
    )
    assert "its content was altered or corrupted" in message
    assert message.endswith(DEVELOPMENT_REMEDY)


def test_a_message_names_hashes_by_their_prefix_and_never_shows_content(
    tree: Tree,
) -> None:
    path = tree.file(CLIP, "model.safetensors")
    flip_one_byte(path)
    found = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = hashlib.sha256(CONTENTS[CLIP]["model.safetensors"]).hexdigest()

    message = refusal(tree.runtime())

    assert f"sha256 {found[:12]}..., expected {expected[:12]}..." in message
    assert found not in message and expected not in message
    assert SECRET.decode() not in message
    assert "\n" not in message


def test_a_model_the_lockfile_does_not_name_is_a_defect_of_the_version(
    tree: Tree,
) -> None:
    assert "names no model 'siglip'" in refusal(tree.runtime(), "siglip")


def test_presence_covers_both_models_and_a_load_only_its_own(tree: Tree) -> None:
    """Section 6.1: the start of a process looks at both; the job runner
    then loads only CLIP, and checks only CLIP's hashes."""
    tree.file(TRANSLATOR, "source.spm").unlink()
    runtime = tree.runtime()

    with pytest.raises(ModelUnavailableError, match="source.spm"):
        runtime.check_presence()
    assert runtime.verified_directory(CLIP) == tree.directory(CLIP)


def test_the_check_of_a_start_never_hashes(tree: Tree, hashed: list[str]) -> None:
    tree.runtime().check_presence()

    assert hashed == []


# --------------------------------------------------------------------------
# Section 5.2: the offline mode is checked on the load
# --------------------------------------------------------------------------


def test_a_load_with_the_network_on_is_refused_before_anything_is_read(
    tree: Tree, hashed: list[str]
) -> None:
    message = refusal(tree.runtime(offline=False))

    assert "huggingface_hub was imported before app.infrastructure.ai" in message
    assert "example/clip was not loaded" in message
    assert hashed == []


def test_in_this_process_the_offline_mode_is_on() -> None:
    """The root `conftest.py` imports the package first, as every program
    of the project does; the check the runtime makes says so too."""
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert model_runtime.huggingface_is_offline() is True


# --------------------------------------------------------------------------
# The second branch of section 6.3: the verification record
# --------------------------------------------------------------------------


def test_a_model_is_hashed_once_and_then_vouched_for_by_the_record(
    tree: Tree, record: Path, hashed: list[str]
) -> None:
    tree.runtime(record_file=record).verified_directory(CLIP)
    assert sorted(hashed) == sorted(CONTENTS[CLIP])

    hashed.clear()
    tree.runtime(record_file=record).verified_directory(CLIP)

    assert hashed == []


def test_the_record_says_what_it_vouches_for(tree: Tree, record: Path) -> None:
    """Section 6.3: the path, the size, the modification time and the NTFS
    file id of each file, and the lockfile they were hashed against."""
    tree.runtime(record_file=record).verified_directory(CLIP)

    written = json.loads(record.read_text(encoding="utf-8"))
    path = tree.file(CLIP, "model.safetensors")
    found = path.stat()

    assert written["lockfile_sha256"] == tree.lockfile.sha256
    assert written["files"][str(path)] == {
        "size": found.st_size,
        "mtime_ns": found.st_mtime_ns,
        "file_id": f"{found.st_dev}:{found.st_ino}",
        "sha256": hashlib.sha256(CONTENTS[CLIP]["model.safetensors"]).hexdigest(),
    }
    assert set(written["files"]) == {
        str(tree.file(CLIP, name)) for name in CONTENTS[CLIP]
    }


def test_both_models_share_one_record(
    tree: Tree, record: Path, hashed: list[str]
) -> None:
    tree.runtime(record_file=record).verified_directory(CLIP)
    tree.runtime(record_file=record).verified_directory(TRANSLATOR)
    hashed.clear()

    runtime = tree.runtime(record_file=record)
    runtime.verified_directory(CLIP)
    runtime.verified_directory(TRANSLATOR)

    assert hashed == []


def test_a_file_rewritten_in_place_is_hashed_again_and_refused(
    tree: Tree, record: Path
) -> None:
    tree.runtime(record_file=record).verified_directory(CLIP)
    path = tree.file(CLIP, "model.safetensors")
    before = path.stat().st_mtime_ns
    flip_one_byte(path)
    os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))

    assert "is not the file this version expects" in refusal(
        tree.runtime(record_file=record)
    )


def test_a_file_replaced_by_a_copy_is_hashed_again(
    tree: Tree, record: Path, tmp_path: Path, hashed: list[str]
) -> None:
    """A new file has a new NTFS id, even with the old size and dates."""
    tree.runtime(record_file=record).verified_directory(CLIP)
    path = tree.file(CLIP, "model.safetensors")
    copy = tmp_path / "copy"
    shutil.copy2(path, copy)
    os.replace(copy, path)
    hashed.clear()

    tree.runtime(record_file=record).verified_directory(CLIP)

    assert hashed == ["model.safetensors"]


def test_what_the_record_gives_up_is_a_change_that_keeps_size_dates_and_id(
    tree: Tree, record: Path
) -> None:
    """The cost section 6.3 declares, pinned so that it stays a decision: a
    corruption that changes neither the size, the dates nor the file is
    caught only by the next full check."""
    tree.runtime(record_file=record).verified_directory(CLIP)
    path = tree.file(CLIP, "model.safetensors")
    found = path.stat()
    flip_one_byte(path)
    os.utime(path, ns=(found.st_atime_ns, found.st_mtime_ns))

    assert tree.runtime(record_file=record).verified_directory(CLIP)
    assert "is not the file" in refusal(tree.runtime(record_file=None))


def test_a_record_of_another_lockfile_vouches_for_nothing(
    tree: Tree, record: Path, hashed: list[str]
) -> None:
    tree.runtime(record_file=record).verified_directory(CLIP)
    written = json.loads(record.read_text(encoding="utf-8"))
    written["lockfile_sha256"] = "0" * 64
    record.write_text(json.dumps(written), encoding="utf-8")
    hashed.clear()

    tree.runtime(record_file=record).verified_directory(CLIP)

    assert sorted(hashed) == sorted(CONTENTS[CLIP])


def test_a_record_that_cannot_be_read_costs_a_hash_and_is_written_again(
    tree: Tree, record: Path, hashed: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    record.parent.mkdir(parents=True)
    record.write_text("{ half a record", encoding="utf-8")

    with app_log(caplog):
        tree.runtime(record_file=record).verified_directory(CLIP)

    assert sorted(hashed) == sorted(CONTENTS[CLIP])
    assert "could not be read" in caplog.text
    assert json.loads(record.read_text(encoding="utf-8"))["files"]


def test_a_record_that_cannot_be_written_is_logged_and_the_load_goes_on(
    tree: Tree, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record = tmp_path / "a folder, not a file"
    record.mkdir()

    with app_log(caplog):
        directory = tree.runtime(record_file=record).verified_directory(CLIP)

    assert directory == tree.directory(CLIP)
    assert "could not be written" in caplog.text


def test_without_a_record_every_load_hashes(tree: Tree, hashed: list[str]) -> None:
    tree.runtime(record_file=None).verified_directory(CLIP)
    tree.runtime(record_file=None).verified_directory(CLIP)

    assert len(hashed) == 2 * len(CONTENTS[CLIP])


def test_a_refused_file_is_not_hashed_again_until_it_changes(
    tree: Tree, record: Path, hashed: list[str]
) -> None:
    """A search answered with 503 must not cost a hash of the weights each
    time; and a tree fixed while the process runs needs no restart."""
    path = tree.file(CLIP, "model.safetensors")
    good = path.read_bytes()
    flip_one_byte(path)
    runtime = tree.runtime(record_file=record)

    first = refusal(runtime)
    hashed.clear()
    assert refusal(runtime) == first
    assert hashed == []

    path.unlink()
    path.write_bytes(good)

    assert runtime.verified_directory(CLIP) == tree.directory(CLIP)
    assert "model.safetensors" in hashed


def test_two_threads_loading_at_once_hash_once(
    tree: Tree, record: Path, hashed: list[str]
) -> None:
    """The race of RFC-026 section 9.1: two first requests both find the
    adapter unloaded. They queue on the runtime, and the second finds the
    record the first wrote."""
    runtime = tree.runtime(record_file=record)
    start = threading.Barrier(2)
    results: list[Path] = []

    def load() -> None:
        start.wait()
        results.append(runtime.verified_directory(CLIP))

    threads = [threading.Thread(target=load) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results == [tree.directory(CLIP)] * 2
    assert sorted(hashed) == sorted(CONTENTS[CLIP])


def test_the_runtime_of_a_layout_keeps_its_record_in_the_users_cache(
    tmp_path: Path,
) -> None:
    where = layout_at(tmp_path / "Program Files" / "SolidVision", tmp_path / "Local")
    tree = make_tree(where.model_runtime)

    runtime = ModelRuntime(
        where.model_runtime,
        tree.lockfile,
        environment=where.environment,
        record_file=where.cache_directory / RECORD_NAME,
        offline=lambda: True,
    )
    runtime.verified_directory(CLIP)

    assert (tmp_path / "Local" / "SolidVision" / "cache" / RECORD_NAME).is_file()


# --------------------------------------------------------------------------
# Section 6.2: the check comes before the load, in the adapters
# --------------------------------------------------------------------------


class NoTranslation(QueryTranslator):
    def to_english(self, text: str) -> str:
        return text


@pytest.fixture()
def loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every `from_pretrained` the adapters reach, replaced by a refusal to
    load anything: what a test sees is whether it was called."""
    called: list[str] = []

    def stand_in(name: str) -> Any:
        def load(source: Any, **kwargs: Any) -> Any:
            called.append(name)
            raise AssertionError(f"{name} was called")

        return load

    prefix = "app.infrastructure.ai."
    for target in (
        "clip_embedding_model.CLIPModel.from_pretrained",
        "clip_embedding_model.AutoProcessor.from_pretrained",
        "query_translator.AutoModelForSeq2SeqLM.from_pretrained",
        "query_translator.AutoTokenizer.from_pretrained",
    ):
        monkeypatch.setattr(prefix + target, stand_in(target.rsplit(".", 2)[1]))
    return called


def test_a_clip_whose_hash_fails_is_never_handed_to_from_pretrained(
    tree: Tree, loads: list[str]
) -> None:
    flip_one_byte(tree.file(CLIP, "model.safetensors"))
    adapter = ClipEmbeddingModel(
        tree.runtime().loader(CLIP),
        translator=NoTranslation(),
        device=torch.device("cpu"),
    )

    with pytest.raises(ModelUnavailableError, match="is not the file"):
        adapter.encode_text("a lake")

    assert loads == []


def test_a_translator_whose_weights_fail_their_hash_is_never_loaded(
    tree: Tree, loads: list[str]
) -> None:
    """Section 6.2, for the translator: nothing is read by `transformers`
    until every file is the one the lockfile names."""
    flip_one_byte(tree.file(TRANSLATOR, "model.safetensors"))
    translator = MarianQueryTranslator(
        tree.runtime().loader(TRANSLATOR), device=torch.device("cpu")
    )

    with pytest.raises(ModelUnavailableError, match="model.safetensors"):
        translator.to_english("propriedade rural com um pequeno lago")

    assert loads == []


def test_a_load_that_fails_anyway_is_one_line_and_its_traceback_is_logged(
    tree: Tree, loads: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Row six of section 6.4: every file checks out, and transformers
    still fails. The line names the directory; the log has the why."""
    adapter = ClipEmbeddingModel(
        tree.runtime().loader(CLIP),
        translator=NoTranslation(),
        device=torch.device("cpu"),
    )

    with app_log(caplog), pytest.raises(ModelUnavailableError) as raised:
        adapter.encode_text("a lake")

    assert str(raised.value) == (
        f"The model in {tree.directory(CLIP)} could not be loaded; the traceback "
        "is in the log."
    )
    assert loads == ["CLIPModel"]
    assert "AssertionError: CLIPModel was called" in caplog.text
    assert "Traceback" not in str(raised.value)


# --------------------------------------------------------------------------
# The lockfile (section 4.1)
# --------------------------------------------------------------------------


def test_the_digest_of_a_lockfile_ignores_how_git_ended_its_lines(tree: Tree) -> None:
    crlf = tree.lockfile_bytes.replace(b"\n", b"\r\n")

    assert lockfile_digest(crlf) == lockfile_digest(tree.lockfile_bytes)
    assert Lockfile.parse(crlf).sha256 == tree.lockfile.sha256


@pytest.mark.parametrize(
    "content",
    [
        b"{}",
        b"[]",
        b'{"clip": {"repository": "r", "revision": "0", "directory": "d", '
        b'"files": {}}}',
        b'{"clip": "no"}',
    ],
)
def test_a_lockfile_without_models_or_files_does_not_parse(content: bytes) -> None:
    with pytest.raises(ValueError):
        Lockfile.parse(content)


def test_the_running_codes_lockfile_is_read_as_package_data() -> None:
    first = read_lockfile()

    assert set(first.models) == {CLIP, TRANSLATOR}
    assert read_lockfile() is first
    assert re.fullmatch(r"[0-9a-f]{64}", first.sha256)


# --------------------------------------------------------------------------


class app_log:  # noqa: N801 -- used as a context manager, like a function
    """Attach `caplog` to the app's logger, which does not propagate."""

    def __init__(self, caplog: pytest.LogCaptureFixture) -> None:
        self._caplog = caplog
        self._logger = logging.getLogger("app")

    def __enter__(self) -> None:
        self._logger.addHandler(self._caplog.handler)

    def __exit__(self, *exc_info: object) -> None:
        self._logger.removeHandler(self._caplog.handler)
