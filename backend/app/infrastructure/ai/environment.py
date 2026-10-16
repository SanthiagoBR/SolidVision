"""The environment of the model libraries, set before they are imported (RFC-035 5.2).

`huggingface_hub` reads `HF_HUB_OFFLINE` and `HF_HOME` once, when it is first
imported, into module constants (`huggingface_hub/constants.py`); putting
either in the environment afterwards changes nothing (section 2.3). The torch
inductor reads `TORCHINDUCTOR_CACHE_DIR` on every call, and creates
`%TEMP%\\torchinductor_<user>` when the variable is missing. So the package
`__init__` calls this before any other import of the package, and the only
importers of `torch` and `transformers` in `backend/app` are inside the
package (`tests/test_model_imports_architecture.py` keeps it that way).

`os.environ` outside `config/`, which RFC-004 reserves for reading the
configuration: this is not the app's configuration, it is the environment of
a library, and it lives in the package of the adapter that uses it -- the
precedent is `discard_libpq_environment()` of RFC-034.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.infrastructure.config.layout import Layout

OFFLINE_VARIABLE = "HF_HUB_OFFLINE"


def huggingface_home(where: Layout) -> Path:
    """What `huggingface_hub` may write: probably nothing, with the offline mode on."""
    return where.cache_directory / "huggingface"


def inductor_cache(where: Layout) -> Path:
    """The folder `torch` creates when a model is loaded through `transformers`."""
    return where.cache_directory / "torchinductor"


def prepare_model_environment(where: Layout) -> None:
    """Turn the network off and keep the libraries' writes in the data root.

    **Overwrites, without looking at what was there.** Installed, the
    environment is not a source of configuration (RFC-034 section 5.2), and an
    `HF_HUB_OFFLINE=0` left by another program must not turn the network back
    on. In a checkout it overwrites by the same rule, so that a path that
    reaches the Hub breaks where somebody reads the traceback, and not only on
    a user's machine (RFC-035 section 5.3).

    Three assignments and nothing else: no directory is created and the disk
    is not touched, because this runs on every import of the package -- the
    `FakeEmbeddingModel` of the fast suite included. The libraries create
    their folders when they need them.
    """
    os.environ[OFFLINE_VARIABLE] = "1"
    os.environ["HF_HOME"] = str(huggingface_home(where))
    os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(inductor_cache(where))
