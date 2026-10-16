"""Offline models, measured (RFC-035 section 15).

The pilot beside this script observed how the models loaded *before* RFC-035.
This measures what RFC-035 built: the tree materialized from the lockfile, the
loads from a directory, the hash checked before them, and what a staged
installation does with all of it on a machine that never reached Hugging
Face -- three empty profile folders and a sentinel in place of the network.

    python measure_offline_models.py > measure_offline_models.log 2>&1

    --list             the checks, and which are opt-in
    --only a,b         just these
    --skip a,b         all the default ones but these
    --with a,b         add opt-in checks
    --work PATH        use this work directory (it is created; default: a
                       temporary one under the system's temp folder)
    --keep             keep the work directory
    --write-reference  write backend/tests/fixtures/rfc035_reference_embeddings.npz
                       from the load by name, once it equals the load by
                       directory bit for bit (check `reference`)

**Cold, without a privilege.** Windows keeps a file's pages in its standby
list long after the file is closed, and emptying that list needs an
administrator. So a cold load here is a load from a copy written with
`FILE_FLAG_NO_BUFFERING`: its pages never entered the cache, and the first
read of them comes from the disk. Each cold copy says how fast it was read,
which is what shows it was cold.

**What is not measured, and why.** Turning the network off for real is
manual (section 15 says so); this prints what to do. The suite at the
starting head was counted before any change (RFC-035 section 16).

Every child has a deadline, and every child's `%TEMP%` is a directory of its
own, empty at the start.

No new dependency: `ctypes`, `urllib`, `zipfile`, `lzma`, `subprocess`,
`numpy` (which `torch` brings) and the project's own scripts.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import json
import mmap
import os
import platform
import re
import shutil
import socket
import socketserver
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKEND = REPO / "backend"
SCRIPTS = REPO / "scripts"
RUNTIME = REPO / "runtime" / "models"
REFERENCE = BACKEND / "tests" / "fixtures" / "rfc035_reference_embeddings.npz"
REAL_HUB = Path.home() / ".cache" / "huggingface" / "hub"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SCRIPTS))

import stage_installation as staging  # noqa: E402

from app.infrastructure.ai.model_runtime import (  # noqa: E402
    MANIFEST_NAME,
    read_lockfile,
)
from dataset_tools.manifest import (  # noqa: E402
    DEMO_CORPUS_ROOT,
    DEMO_MANIFEST_PATH,
    load_manifest,
)
from dataset_tools.materialize import materialize  # noqa: E402

LOCKFILE = read_lockfile()
CLIP_NAME = LOCKFILE.models["clip"].repository
TRANSLATOR_NAME = LOCKFILE.models["translator"].repository
CLIP_DIRECTORY = LOCKFILE.models["clip"].directory
TRANSLATOR_DIRECTORY = LOCKFILE.models["translator"].directory
OTHER_CLIP = (
    REAL_HUB
    / "models--openai--clip-vit-base-patch32"
    / "snapshots"
    / "c237dc49a33fc61debc9276459120b7eac67e7ef"
    / "model.safetensors"
)
"""Another 512-dimensional checkpoint, with the very size of the right one."""

COST_CRITERION = 0.25
"""Declared before the measurement (section 6.3): the full check runs on every
load if it adds at most a quarter of the load of the same model, cold and
warm. Above it, the second branch enters: the verification record."""

CONVERSION_CRITERION_SECONDS = 1.0
"""Declared before the measurement (section 7.4): the translator moves to a
converted `safetensors` only if it loads at least this much faster, cold."""

RUNS = 5
SEARCH_URL = "/api/v1/images/search"
INTERFERING = {
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONOPTIMIZE",
}
LIBRARY_PREFIXES = ("HF_", "HUGGINGFACE_", "TRANSFORMERS_", "TORCHINDUCTOR_")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")

PORTUGUESE_QUERIES = (
    "propriedade rural com um pequeno lago",
    "pequeno lago natural em uma fazenda, nao um tanque artificial",
    "propriedade em encosta com piscina",
    "piscina em uma casa rural, nao um corpo d'agua natural ou artificial",
    "tanques artificiais de piscicultura em um vale",
    "tanques de aquicultura vistos de cima, nao uma piscina ou um lago",
    "galpao industrial ao lado de terras agricolas",
    "galpao ao lado de uma rodovia, nao um galpao dentro de uma cidade",
    "terreno de obra limpo com terra vermelha exposta",
    "terreno de construcao recem limpo com terra vermelha em terracos",
    "cruzamento na cidade com uma concessionaria de carros",
    "cobertura de concessionaria com carros a venda, nao um estacionamento "
    "em uma rua comum",
    "rua comercial com lojas e vitrines",
    "oficinas e pequenos comercios ao longo de uma rua comercial",
    "vista de baixa altitude de fachadas de lojas com pedestres e carros "
    "estacionados",
    "rua residencial ladeada por casas",
    "rua tranquila de bairro com casas de familia e uma oficina",
    "vista geral de uma cidade em encosta com casas densas",
    "cidade suburbana espalhada por colinas onduladas em uma malha irregular",
    "vista ampla de drone de uma cidade em encosta com um grande complexo "
    "de predios",
    "um gato preto descansando dentro de casa",
    "um cachorro olhando diretamente para a camera",
    "uma bicicleta estacionada em uma rua",
    "uma xicara de cafe de ceramica com pires",
    "uma pessoa trabalhando em um notebook dentro de casa",
)
"""The 25 ground-truth queries of RFC-022, in Portuguese, in the order of
`queries.json`. The bake-off of RFC-023 had such a set, and it went with its
scripts; its log kept five of them, which are the first five here, verbatim
and without accents, as the bake-off wrote them. The other twenty were
written for this measurement in the same style (RFC-035 section 16)."""


# --------------------------------------------------------------------------
# What the children run. Each prints one line, `RESULT <json>`.
# --------------------------------------------------------------------------

ENCODE = r"""
import json, sys, time
from pathlib import Path
import app.infrastructure.ai
import numpy as np
import torch
spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if spec.get("threads"):
    torch.set_num_threads(spec["threads"])
import uuid
from transformers import AutoModelForSeq2SeqLM, AutoProcessor, AutoTokenizer
from transformers import CLIPModel
from app.domain.entities.image import Image
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.ai.query_translator import MarianQueryTranslator
from app.infrastructure.ai.model_runtime import default_runtime, CLIP, TRANSLATOR

cpu = torch.device("cpu")
cached = {"cache_dir": spec["hub"], "local_files_only": True}

class ClipByName(ClipEmbeddingModel):
    # The load before RFC-035: by name, resolved through the cache.
    def _ensure_loaded(self):
        if self._model is None:
            name = spec["clip_name"]
            model = CLIPModel.from_pretrained(name, **cached)
            model.eval()
            self._model = model.to(cpu)
            self._processor = AutoProcessor.from_pretrained(name, **cached)
            self._loaded_from = Path(name)
        return self._model, self._processor

class MarianByName(MarianQueryTranslator):
    def _ensure_loaded(self):
        if self._model is None:
            name = spec["translator_name"]
            model = AutoModelForSeq2SeqLM.from_pretrained(name, **cached)
            model.eval()
            self._model = model.to(cpu)
            self._tokenizer = AutoTokenizer.from_pretrained(name, **cached)
        return self._tokenizer, self._model

def image(path):
    p = ImagePath(path)
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, path)),
        device_id=DeviceId(uuid.UUID(int=0)),
        relative_path=p,
        absolute_path=p,
        filename=Path(path).stem,
        extension=Path(path).suffix.lstrip(".").lower(),
    )

def build(way):
    if way == "name":
        translator = MarianByName(lambda: None, device=cpu)
        return ClipByName(lambda: None, translator=translator, device=cpu)
    runtime = default_runtime()
    translator = MarianQueryTranslator(runtime.loader(TRANSLATOR), device=cpu)
    return ClipEmbeddingModel(runtime.loader(CLIP), translator=translator, device=cpu)

def vectors(embeddings):
    return np.asarray([e.values for e in embeddings], dtype=np.float32)

images = [image(path) for path in spec["images"]]
result = {"threads": torch.get_num_threads(), "ways": {}}
arrays = {}
for way in spec["ways"]:
    model = build(way)
    began = time.perf_counter()
    single = vectors([model.encode_image(item) for item in images])
    batches = [images[start:start + 8] for start in range(0, len(images), 8)]
    batched = vectors([v for batch in batches for v in model.encode_images(batch)])
    prompts = [model.build_prompt(text) for text in spec["queries"]]
    texts = vectors([model.encode_text(text) for text in spec["queries"]])
    seconds = round(time.perf_counter() - began, 1)
    result["ways"][way] = {"seconds": seconds, "prompts": prompts}
    arrays[f"{way}_single"] = single
    arrays[f"{way}_batched"] = batched
    arrays[f"{way}_text"] = texts
    del model
np.savez(spec["out"], **arrays)
print("RESULT " + json.dumps(result))
"""

LOAD = r"""
import json, sys, time, hashlib, threading
from pathlib import Path
spec = json.loads(sys.argv[1])
requests = []
if spec["way"] == "name":
    import httpx
    original = httpx.Client.send
    def send(self, request, *a, **k):
        began = time.perf_counter()
        try:
            return original(self, request, *a, **k)
        finally:
            elapsed = round((time.perf_counter() - began) * 1000, 1)
            thread = threading.current_thread().name
            requests.append({"thread": thread, "ms": elapsed})
    httpx.Client.send = send
    import transformers.safetensors_conversion as conversion
    # No side effect on the Hub: never ask it to open a pull request.
    conversion.spawn_conversion = lambda *a, **k: None
else:
    import app.infrastructure.ai
import torch, transformers
from transformers import AutoModelForSeq2SeqLM, AutoProcessor, AutoTokenizer
from transformers import CLIPModel
source = spec["source"]
hashed = 0.0
if spec.get("hash"):
    began = time.perf_counter()
    for path in sorted(Path(source).iterdir()):
        with path.open("rb") as handle:
            hashlib.file_digest(handle, "sha256")
    hashed = time.perf_counter() - began
began = time.perf_counter()
options = {} if spec["way"] == "name" else {"local_files_only": True}
if spec["model"] == "clip":
    model = CLIPModel.from_pretrained(source, **options); model.eval()
    AutoProcessor.from_pretrained(source, **options)
else:
    model = AutoModelForSeq2SeqLM.from_pretrained(source, **options); model.eval()
    AutoTokenizer.from_pretrained(source, **options)
loaded = time.perf_counter() - began
here = threading.current_thread().name
main = [r for r in requests if r["thread"] == here]
print("RESULT " + json.dumps({
    "hash": hashed,
    "load": loaded,
    "requests": len(main),
    "request_seconds": sum(r["ms"] for r in main) / 1000,
    "background_requests": len(requests) - len(main),
}))
"""

COMPARE = r"""
import json, sys
from pathlib import Path
import app.infrastructure.ai
import torch
from transformers import AutoModelForSeq2SeqLM
pickled, converted = Path(sys.argv[1]), Path(sys.argv[2])
load = AutoModelForSeq2SeqLM.from_pretrained
left = load(pickled, local_files_only=True).state_dict()
right = load(converted, local_files_only=True).state_dict()
same = sorted(left) == sorted(right)
same = same and all(torch.equal(left[k], right[k]) for k in left)
weights = sorted(p.name for p in converted.iterdir() if "model" in p.name)
print("RESULT " + json.dumps({"tensors": len(left), "identical": same,
                              "weights": weights}))
"""

HOSTILE = r"""
import json, os
from app.presentation.dependencies import get_embedding_model
vector = get_embedding_model().encode_text("propriedade rural com um pequeno lago")
import huggingface_hub
import huggingface_hub.constants as constants
print("RESULT " + json.dumps({
    "offline": huggingface_hub.is_offline_mode(),
    "hf_home": constants.HF_HOME,
    "hub_cache": constants.HF_HUB_CACHE,
    "endpoint": constants.ENDPOINT,
    "inductor": os.environ.get("TORCHINDUCTOR_CACHE_DIR"),
    "dimension": len(vector.values),
}))
"""

RECORD_COST = r"""
import json, statistics, sys, time
import app.infrastructure.ai
from app.infrastructure.ai.model_runtime import default_runtime
runtime = default_runtime()

def both():
    runtime.verified_directory("clip")
    runtime.verified_directory("translator")

both()
times, presence = [], []
for _ in range(50):
    began = time.perf_counter()
    both()
    times.append(time.perf_counter() - began)
    began = time.perf_counter()
    runtime.check_presence()
    presence.append(time.perf_counter() - began)
print("RESULT " + json.dumps({
    "verified_ms": statistics.median(times) * 1000,
    "presence_ms": statistics.median(presence) * 1000,
}))
"""


# --------------------------------------------------------------------------
# Small things
# --------------------------------------------------------------------------


def say(text: str = "") -> None:
    print(f"    {text}" if text else "")
    sys.stdout.flush()


def seconds(value: float) -> str:
    return f"{value:.3f} s" if value < 10 else f"{value:.1f} s"


def spread(values: Sequence[float], unit: Callable[[float], str] = seconds) -> str:
    if not values:
        return "no samples"
    if len(values) == 1:
        return unit(values[0])
    return (
        f"median {unit(statistics.median(values))} "
        f"(min {unit(min(values))}, max {unit(max(values))}, n={len(values)})"
    )


def mb(count: float) -> str:
    return f"{count / (1024 * 1024):.1f} MB"


def last_lines(text: str, count: int = 3) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " / ".join(lines[-count:]) if lines else "(nothing)"


def tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def tree_state(root: Path) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return state
    for directory, _, files in os.walk(root):
        relative = os.path.relpath(directory, root)
        state[relative + os.sep] = (-1, 0)
        for name in files:
            found = os.stat(os.path.join(directory, name))
            state[os.path.join(relative, name)] = (found.st_size, found.st_mtime_ns)
    return state


def changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        name for name in set(before) & set(after) if before[name] != after[name]
    )
    return (
        [f"+{name}" for name in added]
        + [f"-{name}" for name in removed]
        + [f"~{name}" for name in changed]
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(port: int, path: str, timeout: float = 600.0) -> tuple[int, Any, float]:
    began = time.perf_counter()
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as answer:
            status, body = int(answer.status), json.load(answer)
    except urllib.error.HTTPError as error:
        status, body = int(error.code), json.load(error)
    return status, body, time.perf_counter() - began


class CheckFailedError(Exception):
    """A check's criterion was not met. The message is the result."""


# --------------------------------------------------------------------------
# Cold copies (see the docstring)
# --------------------------------------------------------------------------

_GENERIC_WRITE = 0x40000000
_CREATE_NEW = 1
_NO_BUFFERING = 0x20000000
_WRITE_THROUGH = 0x80000000
_SECTOR = 4096
_CHUNK = 8 * 1024 * 1024


def cold_copy(source: Path, target: Path) -> None:
    """Copy `source` to `target` without a byte of it entering the cache."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    size = source.stat().st_size
    handle = kernel32.CreateFileW(
        str(target),
        _GENERIC_WRITE,
        0,
        None,
        _CREATE_NEW,
        _NO_BUFFERING | _WRITE_THROUGH,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = mmap.mmap(-1, _CHUNK)  # page-aligned, as unbuffered writes need
    anchor = ctypes.c_char.from_buffer(buffer)
    address = ctypes.addressof(anchor)
    try:
        with source.open("rb") as reading:
            while True:
                buffer.seek(0)
                count = reading.readinto(buffer)  # type: ignore[arg-type]
                if not count:
                    break
                padded = -(-count // _SECTOR) * _SECTOR
                if padded != count:
                    buffer[count:padded] = bytes(padded - count)
                written = ctypes.c_uint32(0)
                if not kernel32.WriteFile(
                    handle, address, padded, ctypes.byref(written), None
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
        del anchor
        buffer.close()
    os.truncate(target, size)


def cold_tree(source: Path, target: Path) -> None:
    """A cold copy of a model directory."""
    target.mkdir(parents=True)
    for path in source.iterdir():
        cold_copy(path, target / path.name)


def read_speed(path: Path) -> float:
    began = time.perf_counter()
    with path.open("rb") as handle:
        while handle.read(_CHUNK):
            pass
    return path.stat().st_size / (time.perf_counter() - began) / (1024 * 1024)


# --------------------------------------------------------------------------
# The sentinel: every connection a child tries through its proxy
# --------------------------------------------------------------------------


class Sentinel:
    """A proxy that refuses everything, and remembers what was asked of it."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        sentinel = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    first = self.request.recv(4096).split(b"\r\n", 1)[0]
                    sentinel.attempts.append(first.decode("latin-1", "replace"))
                    self.request.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                    )
                except OSError as error:
                    sentinel.attempts.append(f"(connection, {error})")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------


@dataclass
class Service:
    name: str
    process: subprocess.Popen[bytes]
    stdout: Path
    stderr: Path
    port: int | None = None
    started: float = field(default_factory=time.perf_counter)

    def output(self) -> str:
        return "".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (self.stdout, self.stderr)
            if path.is_file()
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.process.terminate()
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()


class Measurement:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.staged_root = work / "Program Files" / "SolidVision"
        self.home = work / "profile"
        self.local = self.home / "AppData" / "Local"
        self.roaming = self.home / "AppData" / "Roaming"
        self.data = self.local / "SolidVision"
        self.temp = work / "temp"
        self.empty = work / "empty"
        self.out = work / "out"
        self.elsewhere = work / "elsewhere"
        self.corpus = work / "corpus"
        for directory in (self.local, self.roaming, self.temp, self.empty, self.out):
            directory.mkdir(parents=True, exist_ok=True)
        self.sentinel = Sentinel()
        self.services: list[Service] = []
        self.cluster_up = False
        self.default_vectors: Path | None = None

    # -- children --------------------------------------------------------

    def base_env(self) -> dict[str, str]:
        """The caller's environment without what would change the result."""
        return {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in INTERFERING
            and not name.upper().startswith(LIBRARY_PREFIXES)
            and name.upper() not in PROXY_VARIABLES
        }

    def checkout_env(self, **extra: str) -> dict[str, str]:
        environment = self.base_env()
        environment.update(
            PYTHONPATH=str(BACKEND),
            LOCALAPPDATA=str(self.work / "checkout-profile"),
            TEMP=str(self.temp),
            TMP=str(self.temp),
            PYTHONIOENCODING="utf-8",
        )
        environment.update(extra)
        return environment

    def installed_env(self, **extra: str) -> dict[str, str]:
        """A machine that never reached Hugging Face (section 15): three empty
        profile folders, and the sentinel as the only way out."""
        environment = self.base_env()
        environment.update(
            PYTHONPATH=str(self.staged_root / "backend"),
            USERPROFILE=str(self.home),
            LOCALAPPDATA=str(self.local),
            APPDATA=str(self.roaming),
            TEMP=str(self.temp),
            TMP=str(self.temp),
            HTTP_PROXY=self.sentinel.url,
            HTTPS_PROXY=self.sentinel.url,
            ALL_PROXY=self.sentinel.url,
            PYTHONIOENCODING="utf-8",
        )
        environment.update(extra)
        return environment

    def python(
        self,
        arguments: Sequence[str],
        *,
        env: dict[str, str],
        timeout: float = 900.0,
        isolated: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [sys.executable, *(["-I"] if isolated else []), *arguments],
                cwd=self.empty,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise CheckFailedError(
                f"no answer in {timeout:.0f} s from `python {' '.join(arguments[:3])}`"
            ) from None

    @staticmethod
    def answer(done: subprocess.CompletedProcess[str]) -> Any:
        for line in done.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line.removeprefix("RESULT "))
        raise CheckFailedError(
            f"exit {done.returncode} and no result: {last_lines(done.stderr, 6)}"
        )

    def service(
        self, name: str, arguments: Sequence[str], env: dict[str, str]
    ) -> Service:
        stdout, stderr = self.out / f"{name}.stdout", self.out / f"{name}.stderr"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(
                [sys.executable, *arguments],
                cwd=self.empty,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
            )
        started = Service(name, process, stdout, stderr)
        self.services.append(started)
        return started

    def start_api(self, env: dict[str, str], name: str = "api") -> Service:
        port = free_port()
        api = self.service(
            name,
            [
                "-m",
                "uvicorn",
                "app.presentation.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env,
        )
        api.port = port
        return api

    @staticmethod
    def first_health(api: Service, deadline: float = 900.0) -> tuple[int, float]:
        """The first answer of `/health`, and how long after the start it came."""
        assert api.port is not None
        limit = time.monotonic() + deadline
        while True:
            if not api.alive():
                raise CheckFailedError(
                    f"{api.name} exited {api.process.returncode}: "
                    f"{last_lines(api.output(), 6)}"
                )
            try:
                status, _, _ = http_get(api.port, "/health", timeout=30)
                return status, time.perf_counter() - api.started
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= limit:
                    raise CheckFailedError(
                        f"{api.name}: no /health in {deadline:.0f} s"
                    ) from None
                time.sleep(0.1)

    # -- the staged installation and its cluster ---------------------------

    def staging(self) -> Path:
        if not self.staged_root.exists():
            began = time.perf_counter()
            staging.stage(self.staged_root, read_only=True)
            say(
                f"staged {self.staged_root}, read-only, in "
                f"{seconds(time.perf_counter() - began)} "
                f"({mb(tree_bytes(self.staged_root))})"
            )
        return self.staged_root

    def ensure_cluster(self) -> None:
        if self.cluster_up:
            return
        self.staging()
        began = time.perf_counter()
        done = self.python(
            ["-m", "app.infrastructure.database.embedded", "start"],
            env=self.installed_env(),
            timeout=1800,
        )
        if done.returncode != 0 or "ready on" not in done.stderr:
            raise CheckFailedError(
                f"start exited {done.returncode}: {last_lines(done.stderr, 5)}"
            )
        self.cluster_up = True
        say(f"cluster started in {seconds(time.perf_counter() - began)}")

    def stop_everything(self) -> None:
        for started in self.services:
            started.stop()
        if self.cluster_up:
            self.python(
                ["-m", "app.infrastructure.database.embedded", "stop"],
                env=self.installed_env(),
                timeout=300,
            )
            self.cluster_up = False

    def user_file(self, text: str | None) -> None:
        path = self.data / "solidvision.env"
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def corpus_images(self) -> list[Path]:
        """A copy of the demo corpus under the work directory, to be indexed."""
        if not self.corpus.exists():
            materialize(
                load_manifest(DEMO_MANIFEST_PATH), DEMO_CORPUS_ROOT, self.corpus
            )
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        return [self.corpus / image.relative_path for image in manifest.images]

    @staticmethod
    def demo_images() -> list[Path]:
        """The 45 images of RFC-022, where the repository keeps them."""
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        return [DEMO_CORPUS_ROOT / image.relative_path for image in manifest.images]

    # ======================================================================
    # The checks
    # ======================================================================

    def check_materialize(self, hub: bool = False) -> None:
        """`vendor_models.py` into an empty directory, from the cache."""
        sources = [("cache", ["--from-cache"])] + ([("hub", [])] if hub else [])
        for name, options in sources:
            target = self.work / f"materialized-{name}" / "models"
            began = time.perf_counter()
            done = self.python(
                [str(SCRIPTS / "vendor_models.py"), "--target", str(target), *options],
                env={
                    **self.base_env(),
                    "PYTHONIOENCODING": "utf-8",
                    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
                },
                timeout=7200,
            )
            elapsed = time.perf_counter() - began
            if done.returncode != 0:
                raise CheckFailedError(
                    f"{name}: {last_lines(done.stdout + done.stderr, 4)}"
                )
            manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
            say(
                f"from the {name}: {seconds(elapsed)}, {manifest['bytes']} bytes "
                f"({mb(manifest['bytes'])}), lockfile "
                f"{manifest['lockfile_sha256'][:12]}, every file's sha256 equal to "
                "the lockfile's"
            )
            if name == "hub":
                say(
                    "  the lockfile's hashes were checked against the Hub by --lock "
                    "(LFS sha256 for the two weights, git blob id for the 13 others)"
                )
            shutil.rmtree(target.parent)

    def check_size(self) -> None:
        """`runtime\\models` on disk, and compressed two ways."""
        total = tree_bytes(RUNTIME)
        weights = sum(
            entry.size for model in LOCKFILE.models.values() for entry in model.files
        )
        say(
            f"runtime\\models: {total} bytes ({mb(total)}); the lockfile's files "
            f"{weights} bytes, the manifest {total - weights} bytes"
        )
        archive = self.work / "models.zip"
        began = time.perf_counter()
        with zipfile.ZipFile(
            archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zipped:
            for path in sorted(RUNTIME.rglob("*")):
                if path.is_file():
                    zipped.write(path, path.relative_to(RUNTIME.parent))
        say(
            f"ZIP (deflate, level 9): {mb(archive.stat().st_size)} "
            f"({archive.stat().st_size / total:.1%}), in "
            f"{seconds(time.perf_counter() - began)}"
        )
        archive.unlink()
        archive = self.work / "models.tar.xz"
        began = time.perf_counter()
        with tarfile.open(archive, "w:xz", preset=5) as packed:
            packed.add(RUNTIME, arcname="models")
        say(
            "xz (LZMA2, preset 5 -- 7-Zip's default method; 7-Zip is not "
            f"installed here): {mb(archive.stat().st_size)} "
            f"({archive.stat().st_size / total:.1%}), in "
            f"{seconds(time.perf_counter() - began)}"
        )
        archive.unlink()

    def encode(
        self,
        ways: Sequence[str],
        threads: int | None,
        out: Path,
        images: Sequence[Path],
        queries: Sequence[str],
    ) -> Any:
        spec = {
            "ways": list(ways),
            "threads": threads,
            "out": str(out),
            "hub": str(REAL_HUB),
            "clip_name": CLIP_NAME,
            "translator_name": TRANSLATOR_NAME,
            "images": [str(path) for path in images],
            "queries": list(queries),
        }
        spec_file = self.out / f"{out.stem}.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        return self.answer(
            self.python(
                ["-c", ENCODE, str(spec_file)], env=self.checkout_env(), timeout=7200
            )
        )

    def check_reference(self, write: bool) -> None:
        """Section 7.2: by name and by directory, one process, bit for bit."""
        english = [
            entry["query"]
            for entry in json.loads(
                (DEMO_MANIFEST_PATH.parent / "queries.json").read_text(encoding="utf-8")
            )["queries"]
        ]
        if len(english) != len(PORTUGUESE_QUERIES):
            raise CheckFailedError(
                "the Portuguese set is out of step with queries.json"
            )
        queries = english + list(PORTUGUESE_QUERIES)
        images = self.demo_images()
        out = self.out / "reference.npz"
        result = self.encode(["name", "directory"], None, out, images, queries)
        arrays = np.load(out)
        name, directory = result["ways"]["name"], result["ways"]["directory"]
        say(
            f"threads: {result['threads']}; by name {name['seconds']} s, "
            f"by directory {directory['seconds']} s (both models, 45 images twice, "
            "50 queries)"
        )
        failures = []
        for kind, count in (("single", 45), ("batched", 45), ("text", 50)):
            left, right = arrays[f"name_{kind}"], arrays[f"directory_{kind}"]
            same_rows = sum(
                left[i].tobytes() == right[i].tobytes() for i in range(count)
            )
            say(f"{kind:8}: {same_rows} of {count} vectors identical byte for byte")
            if same_rows != count:
                failures.append(kind)
        same_prompts = sum(
            a == b for a, b in zip(name["prompts"], directory["prompts"], strict=True)
        )
        translated = sum(
            prompt != f"a photo of {query}"
            for prompt, query in zip(
                directory["prompts"][25:], PORTUGUESE_QUERIES, strict=True
            )
        )
        say(
            f"prompts : {same_prompts} of 50 identical; {translated} of the 25 "
            "Portuguese queries were detected as Portuguese and translated"
        )
        batch_vs_single = np.abs(
            arrays["directory_single"] - arrays["directory_batched"]
        ).max()
        say(
            "batches of 8 against one at a time: largest difference "
            f"{batch_vs_single:.3g} (RFC-024: equal to within floating point, not "
            "bit for bit)"
        )
        if failures or same_prompts != 50:
            raise CheckFailedError(f"not bit for bit: {failures or 'prompts'}")
        self.default_vectors = out
        if not write:
            return
        metadata = self.reference_metadata()
        manifest = load_manifest(DEMO_MANIFEST_PATH)
        REFERENCE.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            REFERENCE,
            image_paths=np.array([image.relative_path for image in manifest.images]),
            image_vectors=arrays["name_single"],
            image_vectors_batched=arrays["name_batched"],
            queries=np.array(queries),
            prompts=np.array(name["prompts"]),
            query_vectors=arrays["name_text"],
            machine=np.array(json.dumps(metadata, sort_keys=True)),
            lockfile_sha256=np.array(LOCKFILE.sha256),
        )
        say(
            f"wrote {REFERENCE.relative_to(REPO)} ({REFERENCE.stat().st_size} bytes) "
            f"from the load by name, on {metadata['processor']}, "
            f"{metadata['threads']} threads, torch {metadata['torch']}, "
            f"transformers {metadata['transformers']}"
        )

    def reference_metadata(self) -> dict[str, str]:
        done = self.python(
            [
                "-c",
                "import json, os, platform, sys, torch, transformers, numpy; "
                "print('RESULT ' + json.dumps({'processor': platform.processor(), "
                "'cpu_count': os.cpu_count(), 'threads': torch.get_num_threads(), "
                "'torch': torch.__version__, 'transformers': transformers.__version__, "
                "'numpy': numpy.__version__, 'python': sys.version.split()[0]}))",
            ],
            env=self.checkout_env(),
        )
        return {key: str(value) for key, value in self.answer(done).items()}

    def check_threads(self) -> None:
        """Section 7.3: the same load, one thread against the default."""
        if self.default_vectors is None:
            raise CheckFailedError("needs `reference`, which gives the default vectors")
        default = np.load(self.default_vectors)
        out = self.out / "one-thread.npz"
        english = [
            entry["query"]
            for entry in json.loads(
                (DEMO_MANIFEST_PATH.parent / "queries.json").read_text(encoding="utf-8")
            )["queries"]
        ]
        result = self.encode(
            ["directory"],
            1,
            out,
            self.demo_images(),
            english + list(PORTUGUESE_QUERIES),
        )
        single = np.load(out)
        say(f"threads: {result['threads']}, against the default's")
        for kind in ("single", "batched", "text"):
            left, right = default[f"directory_{kind}"], single[f"directory_{kind}"]
            identical = sum(
                left[i].tobytes() == right[i].tobytes() for i in range(len(left))
            )
            cosine = float((left * right).sum(axis=1).min())
            say(
                f"{kind:8}: {identical} of {len(left)} identical; largest difference "
                f"{np.abs(left - right).max():.3g}, smallest cosine {cosine:.9f}"
            )

    def check_database(self) -> None:
        """Section 7.3: the new vectors against the ones in the development
        database, for every row whose file is still on disk, unchanged.
        Informative: those rows may come from another version of torch."""
        import psycopg

        from app.infrastructure.config.settings import settings

        rows = []
        with psycopg.connect(
            host=settings.database_host,
            port=settings.database_port,
            dbname=settings.database_name,
            user=settings.database_user,
            password=settings.database_password,
            connect_timeout=10,
        ) as connection:
            for relative, content_hash, text in connection.execute(
                "select relative_path, content_hash, embedding::text from images "
                "where embedding is not null order by relative_path"
            ):
                path = Path("C:/") / relative
                if not path.is_file():
                    continue
                with path.open("rb") as handle:
                    if (
                        hashlib.file_digest(handle, "sha256").hexdigest()
                        != content_hash
                    ):
                        continue
                vector = np.array(json.loads(text), dtype=np.float32)
                rows.append((path, vector))
        say(
            f"{len(rows)} rows of the development database whose file is on disk, "
            "unchanged"
        )
        if not rows:
            return
        out = self.out / "database.npz"
        self.encode(["directory"], None, out, [path for path, _ in rows], [])
        new = np.load(out)["directory_single"]
        stored = np.stack([vector for _, vector in rows])
        identical = sum(
            new[i].tobytes() == stored[i].tobytes() for i in range(len(rows))
        )
        cosine = (new * stored).sum(axis=1)
        say(
            f"identical: {identical} of {len(rows)}; largest difference "
            f"{np.abs(new - stored).max():.3g}; smallest cosine {cosine.min():.9f}"
        )
        demo = [i for i, (path, _) in enumerate(rows) if "dataset" in path.parts]
        if demo:
            say(
                f"  of which {len(demo)} demo-corpus images: identical "
                f"{sum(new[i].tobytes() == stored[i].tobytes() for i in demo)}, "
                f"smallest cosine {cosine[demo].min():.9f}"
            )

    # -- loading: the cost of the check, and by directory against by name --

    def load_child(
        self,
        way: str,
        model: str,
        source: Path,
        *,
        hash_first: bool = False,
        home: Path | None = None,
    ) -> dict[str, float]:
        name = CLIP_NAME if model == "clip" else TRANSLATOR_NAME
        spec = json.dumps(
            {
                "way": way,
                "model": model,
                "source": name if way == "name" else str(source),
                "hash": hash_first,
            }
        )
        if way == "name":
            assert home is not None
            environment = self.base_env()
            environment.update(
                HF_HOME=str(home),
                TEMP=str(self.temp),
                TMP=str(self.temp),
                TORCHINDUCTOR_CACHE_DIR=str(self.temp / "inductor"),
                PYTHONIOENCODING="utf-8",
                HF_HUB_DISABLE_PROGRESS_BARS="1",
            )
            done = self.python(["-c", LOAD, spec], env=environment, isolated=True)
        else:
            done = self.python(["-c", LOAD, spec], env=self.checkout_env())
        result: dict[str, float] = self.answer(done)
        return result

    def cold_model(self, model: str, label: str) -> Path:
        """A fresh cold copy of one model's directory."""
        source = RUNTIME / (CLIP_DIRECTORY if model == "clip" else TRANSLATOR_DIRECTORY)
        target = self.work / "cold" / f"{model}-{label}" / source.name
        if target.parent.exists():
            shutil.rmtree(target.parent)
        cold_tree(source, target)
        return target

    def cold_hub(self, model: str, label: str) -> Path:
        """An `HF_HOME` holding a cold copy of the pinned snapshot, as a cache.

        The structure a load by name leaves, minus `.no_exist`: `refs/main`
        and the snapshot's files, which is what this machine's cache has.
        """
        locked = LOCKFILE.models[model]
        home = self.work / "cold" / f"hub-{model}-{label}"
        if home.exists():
            shutil.rmtree(home)
        folder = "models--" + locked.repository.replace("/", "--")
        repository = home / "hub" / folder
        cold_tree(
            RUNTIME / locked.directory, repository / "snapshots" / locked.revision
        )
        (repository / "refs").mkdir(parents=True)
        (repository / "refs" / "main").write_text(locked.revision, encoding="ascii")
        # What else this machine's cache holds for the repository, linked and
        # never loaded: the `refs/pr/6` snapshot that `Thread-auto_conversion`
        # downloaded (section 2.2). Without it, every load by name of the
        # translator would download 312 MB again in the background.
        real = REAL_HUB / folder
        for path in real.rglob("*"):
            relative = path.relative_to(real)
            if (
                path.is_file()
                and relative.parts[0] in ("snapshots", "refs")
                and (
                    relative.parts[1] != locked.revision
                    and relative.parts[-1] != "main"
                )
            ):
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(path, target)
        return home

    def check_loads(self) -> None:
        """Sections 6.3 and 15: what the hash costs against the load, and the
        load by directory against the load by name (online), cold and warm."""
        results: dict[str, dict[str, list[dict[str, float]]]] = {}
        for model in ("clip", "translator"):
            keys = (
                "dir cold",
                "hash+dir cold",
                "dir warm",
                "hash+dir warm",
                "name cold",
                "name warm",
            )
            runs: dict[str, list[dict[str, float]]] = {key: [] for key in keys}
            warm = RUNTIME / (
                CLIP_DIRECTORY if model == "clip" else TRANSLATOR_DIRECTORY
            )
            speeds = []
            for run in range(RUNS):
                cold = self.cold_model(model, f"a{run}")
                runs["dir cold"].append(self.load_child("dir", model, cold))
                shutil.rmtree(cold.parent)
                cold = self.cold_model(model, f"b{run}")
                runs["hash+dir cold"].append(
                    self.load_child("dir", model, cold, hash_first=True)
                )
                shutil.rmtree(cold.parent)
                probe = self.cold_model(model, f"c{run}")
                speeds.append(
                    read_speed(max(probe.iterdir(), key=lambda p: p.stat().st_size))
                )
                shutil.rmtree(probe.parent)
                home = self.cold_hub(model, f"{run}")
                runs["name cold"].append(
                    self.load_child("name", model, warm, home=home)
                )
                runs["name warm"].append(
                    self.load_child("name", model, warm, home=home)
                )
                shutil.rmtree(home)
                runs["dir warm"].append(self.load_child("dir", model, warm))
                runs["hash+dir warm"].append(
                    self.load_child("dir", model, warm, hash_first=True)
                )
            results[model] = runs
            say(
                f"{model}: a cold copy's weights read at "
                f"{spread(speeds, lambda v: f'{v:.0f} MB/s')}"
            )
            for key, values in runs.items():
                loads = [value["load"] for value in values]
                extra = ""
                if key.startswith("hash"):
                    extra = (
                        f"; the hash before it {spread([v['hash'] for v in values])}"
                    )
                if key.startswith("name"):
                    asked = statistics.median(v["requests"] for v in values)
                    behind = statistics.median(v["background_requests"] for v in values)
                    waited = spread([v["request_seconds"] for v in values])
                    extra = (
                        f"; {asked:.0f} requests on the loading thread, {waited} "
                        f"waited for them; {behind:.0f} more in the background"
                    )
                say(f"  {key:14} load {spread(loads)}{extra}")
        say()
        for model, runs in results.items():
            cold_load = statistics.median(v["load"] for v in runs["dir cold"])
            cold_both = statistics.median(
                v["hash"] + v["load"] for v in runs["hash+dir cold"]
            )
            warm_load = statistics.median(v["load"] for v in runs["dir warm"])
            warm_hash = statistics.median(v["hash"] for v in runs["hash+dir warm"])
            cold_extra = (cold_both - cold_load) / cold_load
            warm_extra = warm_hash / warm_load
            name_cold = statistics.median(v["load"] for v in runs["name cold"])
            name_warm = statistics.median(v["load"] for v in runs["name warm"])
            verdict = (
                "within" if max(cold_extra, warm_extra) <= COST_CRITERION else "ABOVE"
            )
            say(
                f"{model}: the full check adds {cold_extra:+.0%} cold and "
                f"{warm_extra:+.0%} warm to the load -- {verdict} the criterion of "
                f"{COST_CRITERION:.0%}"
            )
            say(
                f"{model}: by directory {seconds(cold_load)} cold and "
                f"{seconds(warm_load)} warm; by name, online, {seconds(name_cold)} "
                f"and {seconds(name_warm)}"
            )
        cost = self.answer(self.python(["-c", RECORD_COST], env=self.checkout_env()))
        say(
            "the second branch: a load the record vouches for costs "
            f"{cost['verified_ms']:.2f} ms for both models (median of 50); the "
            f"check of a start, {cost['presence_ms']:.2f} ms"
        )

    def check_conversion(self) -> None:
        """Section 7.4: the translator from the `.bin` of its snapshot in the
        cache, and from the `safetensors` `vendor_models.py` converted it to,
        cold and warm.

        The first run of this script, before the conversion entered, compared
        the `.bin` with a `save_pretrained` copy; this compares it with the
        file the tree actually holds.
        """
        locked = LOCKFILE.models["translator"]
        pickled = (
            REAL_HUB
            / ("models--" + locked.repository.replace("/", "--"))
            / "snapshots"
            / locked.revision
        )
        converted = RUNTIME / TRANSLATOR_DIRECTORY
        result = self.answer(
            self.python(
                ["-c", COMPARE, str(pickled), str(converted)], env=self.checkout_env()
            )
        )
        say(
            f"loaded from each: {result['tensors']} tensors, identical: "
            f"{result['identical']}; the tree's weights file is "
            f"{result['weights']}"
        )
        times: dict[str, list[float]] = {
            "bin cold": [],
            "safetensors cold": [],
            "bin warm": [],
            "safetensors warm": [],
        }
        for run in range(RUNS):
            for kind, source in (("bin", pickled), ("safetensors", converted)):
                cold = self.work / "cold" / f"conversion-{kind}-{run}" / source.name
                cold_tree(source, cold)
                times[f"{kind} cold"].append(
                    self.load_child("dir", "translator", cold)["load"]
                )
                shutil.rmtree(cold.parent)
                times[f"{kind} warm"].append(
                    self.load_child("dir", "translator", source)["load"]
                )
        for key, values in times.items():
            say(f"{key:17} {spread(values)}")
        gain = statistics.median(times["bin cold"]) - statistics.median(
            times["safetensors cold"]
        )
        enters = gain >= CONVERSION_CRITERION_SECONDS
        say(
            f"cold, the converted file loads {gain:+.2f} s faster: the conversion "
            f"{'ENTERS' if enters else 'does not enter'} (criterion "
            f"{CONVERSION_CRITERION_SECONDS:.1f} s)"
        )

    # -- the staged installation ------------------------------------------

    def watched(self) -> dict[str, dict[str, tuple[int, int]]]:
        """Everything a child could write to that is not the data root."""
        profile = {
            name: value
            for name, value in tree_state(self.home).items()
            if not name.startswith(str(Path("AppData", "Local", "SolidVision")))
        }
        return {
            "the profile, minus the data root": profile,
            "the children's %TEMP%": tree_state(self.temp),
            "the working directory": tree_state(self.empty),
            "the installed tree": tree_state(self.staged_root),
        }

    def wait_for_log(
        self, started: Service, log: Path, line: str, deadline: float
    ) -> float:
        limit = time.monotonic() + deadline
        while True:
            if log.is_file() and line in log.read_text(
                encoding="utf-8", errors="replace"
            ):
                return time.perf_counter() - started.started
            if not started.alive():
                raise CheckFailedError(
                    f"{started.name} exited {started.process.returncode}: "
                    f"{last_lines(started.output(), 5)}"
                )
            if time.monotonic() >= limit:
                raise CheckFailedError(
                    f"{started.name}: no {line!r} in {deadline:.0f} s"
                )
            time.sleep(0.2)

    def search(self, port: int, query: str) -> tuple[int, Any, float]:
        path = SEARCH_URL + "?" + urllib.parse.urlencode({"q": query, "limit": 5})
        return http_get(port, path)

    def check_offline_install(self) -> None:
        """The roadmap's criterion: a machine that never reached Hugging Face
        indexes the 45 images, and searches in Portuguese, in English and by
        picture -- from a read-only installation, with the sentinel as the
        only way out, and nothing written outside the data root."""
        self.staging()
        self.user_file(None)
        before = self.watched()
        attempts = len(self.sentinel.attempts)
        self.ensure_cluster()
        images = self.corpus_images()
        began = time.perf_counter()
        done = self.python(
            ["-m", "app.cli", "index", "--root", str(self.corpus), "--label", "DEMO"],
            env=self.installed_env(),
            timeout=3600,
        )
        finished = re.search(
            r"finished as (\w+): (\d+) indexed, (\d+) skipped, (\d+) failed",
            done.stderr,
        )
        if done.returncode != 0 or finished is None:
            raise CheckFailedError(
                f"index exited {done.returncode}: {last_lines(done.stderr, 5)}"
            )
        say(
            "index (the CLI, which runs its job in its own process): "
            f"{finished.group(0)}, in {seconds(time.perf_counter() - began)}"
        )

        job_runner = self.service(
            "job_runner",
            ["-m", "app.infrastructure.workers.job_runner"],
            self.installed_env(),
        )
        ready = self.wait_for_log(
            job_runner,
            self.data / "logs" / "job_runner.log",
            "Job executor started",
            900,
        )
        say(f"job runner: in its loop {seconds(ready)} after it started, CLIP loaded")

        api = self.start_api(self.installed_env())
        status, first = self.first_health(api)
        say(
            f"API, installed, no user file: first /health {status} {seconds(first)} "
            "after it started (the warm-up is inside that)"
        )
        log = (self.data / "logs" / "uvicorn.log").read_text(
            encoding="utf-8", errors="replace"
        )
        warmed = re.search(r"Embedding model warmed up in ([\d.]+)s", log)
        say(f"  its warm-up: {warmed.group(1) + ' s' if warmed else 'NOT in the log'}")
        assert api.port is not None
        for query in (
            "propriedade rural com um pequeno lago",
            "rural property with a small lake",
            "um gato preto descansando dentro de casa",
        ):
            status, body, elapsed = self.search(api.port, query)
            top = (
                body["results"][0]["relative_path"]
                if status == 200 and body["results"]
                else "-"
            )
            say(f"search {query!r}: {status} in {elapsed * 1000:.0f} ms, first {top}")
            if status != 200:
                raise CheckFailedError(f"search answered {status}: {body}")

        picture = images[0]
        done = self.python(
            ["-m", "app.cli", "search-image", str(picture), "--limit", "3"],
            env=self.installed_env(),
        )
        if done.returncode != 0:
            raise CheckFailedError(
                f"search-image exited {done.returncode}: "
                f"{last_lines(done.stderr, 4)}"
            )
        say(f"search-image {picture.name}: {done.stdout.splitlines()[0]}")
        job_runner.stop()
        api.stop()

        tried = self.sentinel.attempts[attempts:]
        say(
            f"the sentinel: {len(tried)} attempts" + (f": {tried[:5]}" if tried else "")
        )
        after = self.watched()
        for name in before:
            moved = changes(before[name], after[name])
            found = f"{len(moved)} changes: {moved[:8]}" if moved else "unchanged"
            say(f"{name}: {found}")

        def listing(folder: Path) -> str:
            if not folder.exists():
                return "absent"
            entries = sorted(
                str(path.relative_to(folder)) for path in folder.rglob("*")
            )
            return f"{len(entries)} entries {entries[:6]}"

        cache = self.data / "cache"
        say(f"the data root holds {sorted(path.name for path in self.data.iterdir())}")
        say(f"  cache\\ holds {listing(cache)}")
        if tried:
            raise CheckFailedError(f"{len(tried)} attempts to reach the network")

    def broken_tree(self, case: str) -> Path:
        """A copy of `runtime\\models` with one thing wrong: links for every
        file left as it is, a real copy for the one that changes."""
        root = self.work / "broken" / case / "models"
        if case == "absent":
            return root
        for path in RUNTIME.rglob("*"):
            target = root / path.relative_to(RUNTIME)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(path, target)
        clip = root / CLIP_DIRECTORY / "model.safetensors"
        translator = root / TRANSLATOR_DIRECTORY / "model.safetensors"

        def replace(target: Path, content_from: Path) -> None:
            target.unlink()
            shutil.copyfile(content_from, target)

        if case == "other-lockfile":
            manifest = root / MANIFEST_NAME
            text = json.loads(manifest.read_text(encoding="utf-8"))
            text["lockfile_sha256"] = hashlib.sha256(b"another version").hexdigest()
            manifest.unlink()
            manifest.write_text(json.dumps(text), encoding="utf-8")
        elif case == "deleted":
            (root / CLIP_DIRECTORY / "tokenizer.json").unlink()
        elif case == "truncated":
            clip.unlink()
            with (RUNTIME / CLIP_DIRECTORY / "model.safetensors").open("rb") as source:
                clip.write_bytes(source.read(200 * 1024 * 1024))
        elif case in ("byte-clip", "byte-translator"):
            target = clip if case == "byte-clip" else translator
            replace(target, RUNTIME / target.relative_to(root))
            with target.open("r+b") as handle:
                handle.seek(target.stat().st_size // 2)
                value = handle.read(1)
                handle.seek(-1, os.SEEK_CUR)
                handle.write(bytes([value[0] ^ 0x01]))
        elif case == "other-clip":
            replace(clip, OTHER_CLIP)
        return root

    CASES = (
        "absent",
        "other-lockfile",
        "deleted",
        "truncated",
        "byte-clip",
        "byte-translator",
        "other-clip",
    )
    """Section 15's cases. The `.bin` of the text is gone from the tree since
    the conversion entered (section 7.4): its byte is changed in the
    translator's converted weights instead."""

    def check_conference(self) -> None:
        """Section 6.4 on the staged installation: each way a tree can be
        wrong, and what the job runner, the CLI and the API make of it."""
        self.staging()
        self.ensure_cluster()
        for case in self.CASES:
            root = self.broken_tree(case)
            self.user_file(f"MODEL_RUNTIME_DIRECTORY={root}\n")
            say(f"{case}:")
            for label, arguments in (
                (
                    "job_runner",
                    ["-m", "app.infrastructure.workers.job_runner", "--once"],
                ),
                ("CLI search", ["-m", "app.cli", "search", PORTUGUESE_QUERIES[0]]),
            ):
                done = self.python(arguments, env=self.installed_env())
                lines = done.stderr.strip().splitlines()
                say(
                    f"  {label}: exit {done.returncode}, traceback "
                    f"{'Traceback' in done.stderr}: {(lines or [''])[-1][:300]}"
                )
            api = self.start_api(self.installed_env(), name=f"api-{case}")
            status, _ = self.first_health(api)
            assert api.port is not None
            searched, body, _ = self.search(
                api.port, "propriedade rural com um pequeno lago"
            )
            api.stop()
            output = api.output()
            say(
                f"  API: /health {status}, search {searched}: "
                f"{str(body.get('detail', body))[:300]}; traceback in its output "
                f"{'Traceback' in output}"
            )
            shutil.rmtree(root.parent, ignore_errors=True)
        self.user_file(None)

    def check_hostile(self) -> None:
        """Section 15: the library variables pointed elsewhere, installed."""
        self.staging()
        elsewhere = self.elsewhere
        hostile = {
            "HF_HUB_OFFLINE": "0",
            "TRANSFORMERS_OFFLINE": "0",
            "HF_HOME": str(elsewhere / "hf-home"),
            "HF_HUB_CACHE": str(elsewhere / "hub-cache"),
            "HF_ENDPOINT": self.sentinel.url,
            "TORCHINDUCTOR_CACHE_DIR": str(elsewhere / "inductor"),
        }
        attempts = len(self.sentinel.attempts)
        result = self.answer(
            self.python(
                ["-c", HOSTILE], env=self.installed_env(**hostile), timeout=1800
            )
        )
        say(
            f"offline {result['offline']}; HF_HOME {result['hf_home']}; "
            f"HF_HUB_CACHE {result['hub_cache']}; endpoint {result['endpoint']}; "
            f"TORCHINDUCTOR_CACHE_DIR {result['inductor']}; a "
            f"{result['dimension']}-dimensional vector"
        )
        written = (
            sorted(str(path.relative_to(elsewhere)) for path in elsewhere.rglob("*"))
            if elsewhere.exists()
            else []
        )
        say(
            f"written in the places the variables named: {written or 'nothing'}; "
            f"the sentinel: {len(self.sentinel.attempts) - attempts} attempts"
        )

    # -- the code itself -------------------------------------------------

    def run_tool(
        self, label: str, arguments: Sequence[str], timeout: float = 3600
    ) -> None:
        began = time.perf_counter()
        done = subprocess.run(
            [sys.executable, *arguments],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**self.base_env(), "PYTHONIOENCODING": "utf-8"},
        )
        elapsed = seconds(time.perf_counter() - began)
        say(
            f"{label}: exit {done.returncode} in {elapsed}: "
            f"{last_lines(done.stdout + done.stderr, 2)}"
        )
        if done.returncode != 0:
            raise CheckFailedError(f"{label} failed")

    def check_architecture(self) -> None:
        self.run_tool(
            "the import architecture test",
            [
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "backend/tests/test_model_imports_architecture.py",
                "backend/tests/test_ai_layer_boundaries.py",
                "backend/tests/test_path_anchors_architecture.py",
            ],
        )

    def check_style(self) -> None:
        paths = [
            "backend/app",
            "backend/tests",
            "backend/dataset_tools",
            "scripts/vendor_models.py",
            "scripts/stage_installation.py",
            "scripts/benchmark_indexing.py",
            str(Path(__file__).relative_to(REPO)),
        ]
        self.run_tool("black --check", ["-m", "black", "--check", "--quiet", *paths])
        self.run_tool("ruff check", ["-m", "ruff", "check", "--no-fix", *paths])
        self.run_tool("mypy", ["-m", "mypy"])

    def check_suite(self) -> None:
        self.run_tool("pytest", ["-m", "pytest", "-q", "-p", "no:cacheprovider"])

    def check_slow_suite(self) -> None:
        self.run_tool(
            "pytest -m slow",
            ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "slow"],
            timeout=7200,
        )

    def check_reference_test(self) -> None:
        self.run_tool(
            "the reference test",
            [
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-m",
                "slow",
                "backend/tests/infrastructure/ai/test_reference_embeddings_slow.py",
            ],
            timeout=7200,
        )

    @staticmethod
    def check_network_off() -> None:
        say("manual, by nature (section 15): turn the network off for real -- airplane")
        say("mode -- and, from a staged installation with an empty profile, index the")
        say("demo corpus with `python -m app.cli index`, search in Portuguese and in")
        say("English through the API, and search by picture with `python -m app.cli")
        say("search-image`. This script cannot turn the network off.")


CHECKS: dict[str, tuple[Callable[[Measurement, argparse.Namespace], None], bool]] = {
    "reference": (lambda m, a: m.check_reference(a.write_reference), True),
    "threads": (lambda m, a: m.check_threads(), True),
    "database": (lambda m, a: m.check_database(), True),
    "loads": (lambda m, a: m.check_loads(), True),
    "conversion": (lambda m, a: m.check_conversion(), True),
    "materialize": (lambda m, a: m.check_materialize(), True),
    "hub-download": (lambda m, a: m.check_materialize(hub=True), False),
    "size": (lambda m, a: m.check_size(), True),
    "offline-install": (lambda m, a: m.check_offline_install(), True),
    "conference": (lambda m, a: m.check_conference(), True),
    "hostile": (lambda m, a: m.check_hostile(), True),
    "architecture": (lambda m, a: m.check_architecture(), True),
    "style": (lambda m, a: m.check_style(), True),
    "reference-test": (lambda m, a: m.check_reference_test(), False),
    "suite": (lambda m, a: m.check_suite(), False),
    "slow-suite": (lambda m, a: m.check_slow_suite(), False),
    "network-off": (lambda m, a: m.check_network_off(), True),
}
"""Name -> (what it runs, whether it runs by default)."""


def describe_machine() -> None:
    import torch
    import transformers

    say(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    say(
        f"{platform.platform()}, {platform.processor()}, {os.cpu_count()} logical "
        f"CPUs, Python {sys.version.split()[0]}, torch {torch.__version__} "
        f"({torch.get_num_threads()} threads), transformers {transformers.__version__}"
    )
    say(
        f"lockfile {LOCKFILE.sha256[:12]}; runtime\\models "
        f"{'present' if (RUNTIME / MANIFEST_NAME).is_file() else 'ABSENT'}"
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--skip", default="")
    parser.add_argument("--with", dest="extra", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--write-reference", action="store_true")
    arguments = parser.parse_args()
    if arguments.list:
        for name, (_, default) in CHECKS.items():
            print(f"{name:16} {'default' if default else 'opt-in'}")
        return 0

    def names(text: str) -> list[str]:
        chosen = [name.strip() for name in text.split(",") if name.strip()]
        unknown = [name for name in chosen if name not in CHECKS]
        if unknown:
            raise SystemExit(f"unknown checks: {unknown}")
        return chosen

    if arguments.only:
        selected = names(arguments.only)
    else:
        skipped = set(names(arguments.skip))
        selected = [
            name
            for name, (_, default) in CHECKS.items()
            if default and name not in skipped
        ] + names(arguments.extra)
    work = arguments.work or Path(tempfile.mkdtemp(prefix="rfc035-"))
    work.mkdir(parents=True, exist_ok=True)
    print("RFC-035 -- offline models, measured")
    describe_machine()
    say(f"work directory {work}; checks: {', '.join(selected)}")
    measurement = Measurement(work)
    failed = []
    try:
        for name in selected:
            print(f"\n== {name}")
            began = time.perf_counter()
            try:
                CHECKS[name][0](measurement, arguments)
                say(f"[{name}: done in {seconds(time.perf_counter() - began)}]")
            except CheckFailedError as error:
                failed.append(name)
                say(f"FAILED: {error}")
            except Exception:  # noqa: BLE001 -- a broken check must not stop the rest
                failed.append(name)
                say("FAILED with an error:")
                print(traceback.format_exc())
    finally:
        measurement.stop_everything()
        measurement.sentinel.close()
        if not arguments.keep:
            if measurement.staged_root.exists():
                try:
                    staging.remove(measurement.staged_root)
                except staging.StagingError as error:
                    say(f"the staging was left in place: {error}")
            shutil.rmtree(work, ignore_errors=True)
    print(
        f"\n{len(selected) - len(failed)} of {len(selected)} checks done"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
