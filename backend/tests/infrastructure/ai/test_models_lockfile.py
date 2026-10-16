"""The lockfile is code, and this is its review (RFC-035 section 4.1).

`models.lock.json` decides which model the app loads: changing it is changing
the embedding space of every vector in the database, which is a new RFC with
a migration and a reindexing plan (section 4.4). These tests make such a
change visible in a diff of this file as well as of the lockfile.
"""

from __future__ import annotations

import json
import re
from importlib import resources

import pytest

from app.infrastructure.ai.model_runtime import (
    CLIP,
    LOCKFILE_NAME,
    TRANSLATOR,
    lockfile_digest,
    read_lockfile,
)

RAW = json.loads(
    (resources.files("app.infrastructure.ai") / LOCKFILE_NAME).read_text(
        encoding="utf-8"
    )
)


def test_the_lockfile_names_the_two_models_and_nothing_else() -> None:
    assert set(RAW) == {CLIP, TRANSLATOR}


@pytest.mark.parametrize("key", [CLIP, TRANSLATOR])
def test_every_model_is_pinned_to_a_commit_and_every_file_to_a_hash(key: str) -> None:
    model = RAW[key]

    assert re.fullmatch(r"[0-9a-f]{40}", model["revision"]), "a commit, never main"
    assert model["files"]
    for name, entry in model["files"].items():
        assert isinstance(entry["size"], int) and entry["size"] > 0, name
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), name


def test_every_model_has_a_directory_of_its_own() -> None:
    directories = [model["directory"] for model in RAW.values()]

    assert len(set(directories)) == len(directories)
    for directory in directories:
        assert re.fullmatch(r"[a-z0-9][a-z0-9.-]*", directory), "short, lowercase"


def test_the_models_are_the_ones_rfc_023_selected() -> None:
    """The checks the adapters' tests made by name before RFC-035."""
    assert RAW[CLIP]["repository"] == "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
    assert RAW[CLIP]["revision"] == "1a25a446712ba5ee05982a381eed697ef9b435cf"
    assert RAW[TRANSLATOR]["repository"] == "Helsinki-NLP/opus-mt-ROMANCE-en"
    assert RAW[TRANSLATOR]["revision"] == "e9ca9975e3972afd80732f08ce01d3a1339f47f8"


def test_the_files_are_those_a_load_consults_and_no_other() -> None:
    """Section 2.1: the eight files of the CLIP snapshot and the seven of the
    translator's -- its weights converted to `safetensors` (section 7.4) --
    923 879 112 bytes in all."""
    assert sorted(RAW[CLIP]["files"]) == [
        "config.json",
        "merges.txt",
        "model.safetensors",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
    ]
    assert sorted(RAW[TRANSLATOR]["files"]) == [
        "config.json",
        "generation_config.json",
        "model.safetensors",
        "source.spm",
        "target.spm",
        "tokenizer_config.json",
        "vocab.json",
    ]
    sizes = [
        entry["size"] for model in RAW.values() for entry in model["files"].values()
    ]
    assert sum(sizes) == 923_879_112


def test_the_translator_is_the_pickle_of_main_converted_and_no_pickle_ships() -> None:
    """Section 7.4, second branch: the converted file loaded 2.2 s faster,
    cold. It is made from the `pytorch_model.bin` of the pinned `main` -- whose
    hash the lockfile keeps, to be checked before it is unpickled -- and not
    taken from the `safetensors` of `refs/pr/6`, a bot's pull request."""
    model = RAW[TRANSLATOR]
    recipe = model["converted"]["model.safetensors"]

    assert recipe["from"] == "pytorch_model.bin"
    assert recipe["size"] == 312_087_009
    assert recipe["sha256"].startswith("9d77bbbd43a2")
    assert recipe["tied"] == [
        "model.decoder.embed_tokens.weight",
        "model.encoder.embed_tokens.weight",
    ]
    assert not [name for name in model["files"] if name.endswith((".bin", ".pt"))]
    assert "converted" not in RAW[CLIP]


def test_each_model_carries_the_license_it_is_distributed_under() -> None:
    """Section 4.1: the SolidVision now distributes the weights, and the
    installer needs both licenses. Read from the model cards by `--lock`."""
    assert RAW[CLIP]["license"] == "mit"
    assert RAW[TRANSLATOR]["license"] == "apache-2.0"


def test_the_parsed_lockfile_is_the_file() -> None:
    lockfile = read_lockfile()
    content = (resources.files("app.infrastructure.ai") / LOCKFILE_NAME).read_bytes()

    assert lockfile.sha256 == lockfile_digest(content)
    for key, model in lockfile.models.items():
        assert {locked.name for locked in model.files} == set(RAW[key]["files"])
        assert model.directory == RAW[key]["directory"]
