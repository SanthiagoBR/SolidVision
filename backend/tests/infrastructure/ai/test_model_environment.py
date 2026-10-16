"""The environment of the model libraries (RFC-035 sections 2.3, 5.2 and 5.4).

`huggingface_hub` reads `HF_HUB_OFFLINE` and `HF_HOME` into constants the
first time it is imported, so what matters is the environment of a fresh
process at that moment. The tests that say what a process ends up with
therefore run one: a child with a hostile environment, whose only job is to
import the package the way every program of the project does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.infrastructure.ai.model_trees import make_tree

from app.infrastructure.ai.environment import prepare_model_environment
from app.infrastructure.config.layout import layout_at

BACKEND = Path(__file__).resolve().parents[3]
CHILD_SECONDS = 180


def hostile(tmp_path: Path) -> dict[str, str]:
    """What another program on the machine may have left in the environment."""
    elsewhere = tmp_path / "elsewhere"
    return {
        "HF_HUB_OFFLINE": "0",
        "TRANSFORMERS_OFFLINE": "0",
        "HF_HOME": str(elsewhere / "hf-home"),
        "TORCHINDUCTOR_CACHE_DIR": str(elsewhere / "inductor"),
    }


def run_child(code: str, environment: dict[str, str], *args: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", code, *args],
        env={**os.environ, "PYTHONPATH": str(BACKEND), **environment},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=CHILD_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed.stdout.strip().splitlines()[-1]


# --------------------------------------------------------------------------
# The function
# --------------------------------------------------------------------------


def test_the_three_variables_are_overwritten_and_point_at_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in hostile(tmp_path).items():
        monkeypatch.setenv(name, value)
    where = layout_at(tmp_path / "Program Files" / "SolidVision", tmp_path / "Local")

    prepare_model_environment(where)

    data = tmp_path / "Local" / "SolidVision"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HOME"] == str(data / "cache" / "huggingface")
    assert os.environ["TORCHINDUCTOR_CACHE_DIR"] == str(
        data / "cache" / "torchinductor"
    )


def test_it_touches_no_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs on every import of the package, the fast suite's included."""
    for name in ("HF_HUB_OFFLINE", "HF_HOME", "TORCHINDUCTOR_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    where = layout_at(tmp_path / "root", tmp_path / "Local")

    prepare_model_environment(where)

    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# A process
# --------------------------------------------------------------------------

REPORT = """
import json, os
import app.infrastructure.ai
import huggingface_hub, huggingface_hub.constants as constants
print(json.dumps({
    "offline": huggingface_hub.is_offline_mode(),
    "hf_home": constants.HF_HOME,
    "hub_cache": constants.HF_HUB_CACHE,
    "inductor": os.environ.get("TORCHINDUCTOR_CACHE_DIR"),
}))
"""


def test_a_process_is_offline_whatever_its_environment_says(tmp_path: Path) -> None:
    """Section 9, first line: `HF_HUB_OFFLINE=0`, `TRANSFORMERS_OFFLINE=0`
    and a hostile `HF_HOME`, and the package wins, because it comes first."""
    local = tmp_path / "Local"
    local.mkdir()

    found = json.loads(
        run_child(REPORT, {**hostile(tmp_path), "LOCALAPPDATA": str(local)})
    )

    cache = local / "SolidVision-dev" / "cache"
    assert found["offline"] is True
    assert found["hf_home"] == str(cache / "huggingface")
    assert found["hub_cache"] == str(cache / "huggingface" / "hub")
    assert found["inductor"] == str(cache / "torchinductor")
    assert not (tmp_path / "elsewhere").exists(), "nothing was written there"
    assert not cache.exists(), "importing creates no directory"


IMPORTED_TOO_EARLY = """
import sys
from pathlib import Path
import huggingface_hub.constants
import app.infrastructure.ai
from app.infrastructure.ai.model_runtime import (
    Lockfile, ModelRuntime, ModelUnavailableError,
)
from app.infrastructure.config.layout import Environment
root, lockfile = Path(sys.argv[1]), Path(sys.argv[2])
runtime = ModelRuntime(
    root, Lockfile.parse(lockfile.read_bytes()), Environment.DEVELOPMENT, None
)
try:
    runtime.verified_directory("clip")
except ModelUnavailableError as error:
    print(error)
else:
    print("loaded")
"""


def test_a_process_that_imported_the_library_first_refuses_to_load(
    tmp_path: Path,
) -> None:
    """Section 5.2: the conference after the import. Something that imports
    `huggingface_hub` before the package -- a `sitecustomize`, a tool, a
    test -- freezes the network on, and the load says so instead of going
    ahead with it.

    `huggingface_hub.constants` and not the bare package: `huggingface_hub`
    attaches its submodules lazily, and the constant is computed when
    `constants` is first loaded -- which `import transformers` does, and a
    bare `import huggingface_hub` does not (measured; section 16 of the RFC
    records it)."""
    tree = make_tree(tmp_path / "models")
    lockfile = tmp_path / "models.lock.json"
    lockfile.write_bytes(tree.lockfile_bytes)
    environment = {
        **hostile(tmp_path),
        "HF_HUB_OFFLINE": "",
        "TRANSFORMERS_OFFLINE": "",
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }

    line = run_child(IMPORTED_TOO_EARLY, environment, str(tree.root), str(lockfile))

    assert "huggingface_hub was imported before app.infrastructure.ai" in line
    assert "example/clip was not loaded" in line
