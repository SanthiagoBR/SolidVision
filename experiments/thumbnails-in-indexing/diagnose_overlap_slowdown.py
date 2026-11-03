"""Why does CLIP inference slow down when the disk reads overlap with it?

`measure_prefetch_overlap.py` (2026-10-27) moved discovery and hashing onto
a reader thread and cut a cold index of the real archive from 0.552 to 0.393
s/img -- but inference itself went from ~211 to ~294 ms/img, which turned
the overlapped pipeline CPU-bound and gave back most of what was left to
gain. Four explanations fit that number; each arm here is built to separate
one of them from the others:

* **Cache.** The photos read ahead are evicted before CLIP opens them (this
  machine has 7.7 GB and ~1 GB available), so CLIP reads them from the disk
  a second time, against the reader's seeks. Every arm *probes* each batch
  just before it is encoded: the main thread reads the files through and
  times it -- a millisecond or two from the cache, tens from the disk. After
  the probe the files are in the cache whatever happened before, so the
  encode time that remains is CPU time.
* **GIL.** The reader's Python work -- Pillow's EXIF parsing, building the
  entities -- holds the GIL that the main thread needs between torch calls.
  `prefetch-io` leaves the reader only the full read and SHA-256, which both
  release the GIL on large buffers, and does `stat` + EXIF on the main
  thread, from the cache the reader filled.
* **A shared core.** torch runs 4 intra-op threads, one per physical core,
  and the reader lands on the hyperthread sibling of one of them; an
  OpenMP region is as slow as its slowest thread. `threads=3` leaves a
  whole core to the reader.
* **Clock and Defender.** A 15 W i5-1035G1 trades frequency for load, and
  `MsMpEng` scans files as they are opened. Windows counters are sampled
  every second through every arm: actual MHz (% performance x nominal),
  Defender's CPU, available memory and the standby cache.

No thumbnails anywhere: the slowdown was the same with and without them, and
leaving them out removes a variable. Window and lookahead 64, as before.

Run it from the repository root:

    .venv/Scripts/python.exe \
        experiments/thumbnails-in-indexing/diagnose_overlap_slowdown.py --root E:/
"""

from __future__ import annotations

import argparse
import base64
import gc
import hashlib
import json
import os
import platform
import queue
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from measure_pipeline_without_thumbnails import (
    MEGABYTE,
    SampledFiles,
    TimedHasher,
    list_supported,
)
from measure_prefetch_overlap import (
    PrecomputedHasher,
    PrefetchedCandidates,
    SteadyStateClock,
)

import torch

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexOrUpdateImagesUseCase,
)
from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.filesystem_image_provider import (
    discovery_sort_key,
)
from app.infrastructure.filesystem.sha256_content_hasher import READ_CHUNK_BYTES
from app.infrastructure.filesystem.volume_identity_provider import (
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.persistence.in_memory_device_repository import (
    InMemoryDeviceRepository,
)
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.workers.indexing_worker import (
    discovered_candidates,
    register_device,
)
from app.presentation.dependencies import get_embedding_model

_END = object()
CACHE_MISS_MS = 15.0
"""A probe slower than this read at least part of the file from the disk.

A 4 MB file comes out of the standby cache in 1-3 ms on this machine; the
external disk cannot deliver it in under ~40 ms even sequentially.
"""

COUNTERS = {
    "performance": r"\Informações do Processador(_Total)\% de Desempenho do Processador",
    "frequency": r"\Informações do Processador(_Total)\Frequência do Processador",
    "defender": r"\Processo(MsMpEng)\% tempo de processador",
    "available_mb": r"\Memória\MBytes disponíveis",
    "standby": r"\Memória\Bytes de Prioridade Normal de Cache em Espera",
}
"""Windows counters, by their localized (pt-BR) names, which is what PDH takes."""


class CounterSampler:
    """`Get-Counter` once a second in a PowerShell child, as epoch;values lines.

    A separate process, so that sampling costs this process nothing and
    cannot itself be the contention being measured.
    """

    def __init__(self, output: Path) -> None:
        self._output = output
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> CounterSampler:
        paths = ",".join(f"'{path}'" for path in COUNTERS.values())
        script = (
            f"$out = '{self._output}'\n"
            f"Get-Counter -Counter @({paths}) -SampleInterval 1 -Continuous | "
            "ForEach-Object {\n"
            "  $t = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()\n"
            "  $v = foreach ($s in $_.CounterSamples) "
            "{ $s.CookedValue.ToString([Globalization.CultureInfo]::InvariantCulture) }\n"
            "  Add-Content -Path $out -Value (\"$t;\" + ($v -join ';'))\n"
            "}\n"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        self._process = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.wait()

    def averages(self, start: float, end: float) -> dict[str, float]:
        """Mean of every counter over the samples taken in `[start, end]`."""
        names = list(COUNTERS)
        rows: list[list[float]] = []
        if not self._output.exists():
            return {}
        for line in self._output.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split(";")
            if len(parts) != len(names) + 1:
                continue
            try:
                stamp = int(parts[0]) / 1000.0
                values = [float(value) for value in parts[1:]]
            except ValueError:
                continue
            if start <= stamp <= end:
                rows.append(values)
        if not rows:
            return {}
        means = {name: statistics.fmean(row[i] for row in rows) for i, name in enumerate(names)}
        means["mhz"] = means["performance"] * means["frequency"] / 100.0
        means["samples"] = float(len(rows))
        return means


class ProbingModel:
    """The CLIP adapter, with each batch read through and timed before it is encoded."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.probe_ms: list[float] = []
        self.encode_seconds = 0.0

    def _probe(self, images: Sequence[Image]) -> None:
        for image in images:
            started = time.perf_counter()
            with image.require_absolute_path().value.open("rb") as stream:
                while stream.read(READ_CHUNK_BYTES):
                    pass
            self.probe_ms.append((time.perf_counter() - started) * 1000.0)

    def encode_images(self, images: Sequence[Image]) -> list[object]:
        self._probe(images)
        started = time.perf_counter()
        try:
            return self._inner.encode_images(images)  # type: ignore[attr-defined]
        finally:
            self.encode_seconds += time.perf_counter() - started

    def encode_image(self, image: Image) -> object:
        self._probe([image])
        started = time.perf_counter()
        try:
            return self._inner.encode_image(image)  # type: ignore[attr-defined]
        finally:
            self.encode_seconds += time.perf_counter() - started


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


class RawReadAhead:
    """A reader that only reads and hashes; `stat` and EXIF stay on the main thread.

    Yields paths, which `SampledFiles` turns into discovered files on the
    consuming thread -- from the cache, because the reader has just read
    them through.
    """

    def __init__(self, paths: list[Path], lookahead: int) -> None:
        self._paths = paths
        self._queue: queue.Queue[object] = queue.Queue(maxsize=lookahead)
        self.digests: dict[str, str | BaseException] = {}
        self.hashing_seconds = 0.0
        self.waiting_seconds = 0.0

    def _read_ahead(self) -> None:
        try:
            for path in self._paths:
                started = time.perf_counter()
                try:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        while chunk := stream.read(READ_CHUNK_BYTES):
                            digest.update(chunk)
                    result: str | BaseException = digest.hexdigest()
                except Exception as exc:
                    result = exc
                self.hashing_seconds += time.perf_counter() - started
                self.digests[_key(path)] = result
                self._queue.put(path)
        finally:
            self._queue.put(_END)

    def __iter__(self) -> Iterator[Path]:
        reader = threading.Thread(target=self._read_ahead, daemon=True)
        reader.start()
        while True:
            started = time.perf_counter()
            item = self._queue.get()
            self.waiting_seconds += time.perf_counter() - started
            if item is _END:
                break
            yield item  # type: ignore[misc]
        reader.join()


class PathKeyedHasher(ContentHasherPort):
    def __init__(self, digests: dict[str, str | BaseException]) -> None:
        self._digests = digests

    def hash_image(self, image: Image) -> str:
        digest = self._digests.pop(_key(image.require_absolute_path().value))
        if isinstance(digest, BaseException):
            raise digest
        return digest


@dataclass
class ArmResult:
    arm: int
    mode: str
    threads: int
    files: int
    failed: int
    megabytes: float
    elapsed_seconds: float
    steady_seconds_per_image: float
    discovery_ms: float
    hashing_ms: float
    waiting_ms: float
    probe_ms_mean: float
    probe_ms_median: float
    probe_miss_percent: float
    encode_ms: float
    counters: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.mode}/t{self.threads}"


def run_arm(
    arm: int,
    mode: str,
    threads: int,
    paths: list[Path],
    device: object,
    mount_point: Path,
    model: object,
    window: int,
    lookahead: int,
    sampler: CounterSampler | None,
) -> ArmResult:
    undiscoverable: list[IndexingFailure] = []
    waiting = lambda: 0.0  # noqa: E731
    if mode == "serial":
        provider = SampledFiles(paths)
        timed = TimedHasher()
        hasher: ContentHasherPort = timed
        hashing = lambda: timed.seconds  # noqa: E731
        candidates: object = discovered_candidates(
            provider, device, mount_point, undiscoverable  # type: ignore[arg-type]
        )
    elif mode == "prefetch-full":
        provider = SampledFiles(paths)
        full = PrefetchedCandidates(provider, device, mount_point, undiscoverable, lookahead)
        hasher = PrecomputedHasher(full.digests)
        hashing = lambda: full.hashing_seconds  # noqa: E731
        waiting = lambda: full.waiting_seconds  # noqa: E731
        candidates = full
    elif mode == "prefetch-io":
        raw = RawReadAhead(paths, lookahead)
        provider = SampledFiles(raw)  # type: ignore[arg-type]
        hasher = PathKeyedHasher(raw.digests)
        hashing = lambda: raw.hashing_seconds  # noqa: E731
        waiting = lambda: raw.waiting_seconds  # noqa: E731
        candidates = discovered_candidates(
            provider, device, mount_point, undiscoverable  # type: ignore[arg-type]
        )
    else:
        raise ValueError(mode)

    probing = ProbingModel(model)
    use_case = IndexOrUpdateImagesUseCase(
        repository=InMemoryImageRepository(),
        embedding_model=probing,  # type: ignore[arg-type]
        content_hasher=hasher,
        batch_size=settings.batch_size,
        metadata_prefetch_size=window,
    )
    clock = SteadyStateClock()
    torch.set_num_threads(threads)
    gc.collect()
    wall_start = time.time()
    started = time.perf_counter()
    try:
        summary = use_case.execute(candidates, observer=clock)  # type: ignore[arg-type]
    finally:
        torch.set_num_threads(DEFAULT_THREADS)
    finished = time.perf_counter()
    wall_end = time.time()

    settled = summary.indexed + summary.failed
    if clock.first_window_at is not None and settled > clock.settled_at_first_window:
        steady = (finished - clock.first_window_at) / (settled - clock.settled_at_first_window)
    else:
        steady = (finished - started) / len(paths)

    probes = probing.probe_ms or [0.0]
    per_image = 1000.0 / len(paths)
    return ArmResult(
        arm=arm,
        mode=mode,
        threads=threads,
        files=len(paths),
        failed=summary.failed + len(undiscoverable),
        megabytes=provider.bytes / MEGABYTE,
        elapsed_seconds=summary.elapsed_seconds,
        steady_seconds_per_image=steady,
        discovery_ms=provider.seconds * per_image,
        hashing_ms=hashing() * per_image,
        waiting_ms=waiting() * per_image,
        probe_ms_mean=statistics.fmean(probes),
        probe_ms_median=statistics.median(probes),
        probe_miss_percent=100.0 * sum(ms > CACHE_MISS_MS for ms in probes) / len(probes),
        encode_ms=probing.encode_seconds * per_image,
        counters=sampler.averages(wall_start, wall_end) if sampler is not None else {},
    )


def print_arm(result: ArmResult) -> None:
    counters = result.counters
    print(
        f"  {result.arm:>3}  {result.label:<18} {1 / result.steady_seconds_per_image:>6.2f}"
        f"  {result.discovery_ms:>5.0f} {result.hashing_ms:>5.0f} {result.waiting_ms:>5.0f}"
        f"  {result.probe_ms_mean:>6.1f} {result.probe_ms_median:>6.1f} {result.probe_miss_percent:>5.1f}%"
        f"  {result.encode_ms:>6.0f}"
        f"  {counters.get('mhz', 0):>6.0f} {counters.get('defender', 0):>6.1f}"
        f" {counters.get('available_mb', 0):>7.0f} {counters.get('standby', 0) / MEGABYTE:>7.0f}"
        f"  {result.failed:>3}"
    )


DEFAULT_THREADS = torch.get_num_threads()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--per-arm", type=int, default=192)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--lookahead", type=int, default=None)
    parser.add_argument("--warm-up", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    lookahead = args.lookahead if args.lookahead is not None else args.window

    root = args.root.resolve()
    print(f"python {platform.python_version()}, torch {torch.__version__}, {platform.platform()}")
    print(
        f"processor: {platform.processor()}, {os.cpu_count()} logical CPUs, "
        f"torch intra-op threads {DEFAULT_THREADS}"
    )
    print(
        f"batch_size {settings.batch_size}, window {args.window}, lookahead {lookahead}, "
        f"no thumbnails, cache miss = probe over {CACHE_MISS_MS:.0f} ms"
    )

    listed = list_supported(root)
    configs = [
        ("serial", DEFAULT_THREADS),
        ("prefetch-full", DEFAULT_THREADS),
        ("prefetch-io", DEFAULT_THREADS),
        ("prefetch-full", DEFAULT_THREADS - 1),
    ]
    plan = configs * args.rounds
    sample = random.Random(args.seed).sample(listed, args.warm_up + len(plan) * args.per_arm)
    warm_up, measured = sample[: args.warm_up], sample[args.warm_up :]
    measured.sort(key=lambda path: discovery_sort_key(path.relative_to(root)))
    groups = [measured[arm :: len(plan)] for arm in range(len(plan))]
    print(
        f"listed {len(listed)}; sample {args.warm_up} warm-up + {len(plan)} arms x "
        f"{args.per_arm} from {len({p.parent for p in measured})} folders (seed {args.seed})"
    )

    device, volume = register_device(
        volume_provider=WindowsVolumeIdentityProvider(),
        device_repository=InMemoryDeviceRepository(),
        root=root,
        label="",
        persist=False,
    )
    model = get_embedding_model()
    model.warm_up()

    counter_file = Path(tempfile.mkdtemp(prefix="solidvision-diagnose-")) / "counters.csv"
    results: list[ArmResult] = []
    with CounterSampler(counter_file) as sampler:
        warm = run_arm(-1, "serial", DEFAULT_THREADS, warm_up, device, volume.mount_point,
                       model, args.window, lookahead, None)
        print(f"warm-up: {warm.files} files in {warm.elapsed_seconds:.1f} s (not measured)")
        print()
        print("  arm  config             steady  ---- ms per image ----  ---- probe ms ------  encode   ---- counters (mean) --------  fail")
        print("                          img/s    disc  hash  wait    mean median  miss     ms      MHz  defnd%   availMB standbyMB")
        for arm, ((mode, threads), paths) in enumerate(zip(plan, groups, strict=True)):
            result = run_arm(arm, mode, threads, paths, device, volume.mount_point,
                             model, args.window, lookahead, sampler)
            results.append(result)
            print_arm(result)
            sys.stdout.flush()

    print()
    print("pooled by config")
    summary: dict[str, dict[str, float]] = {}
    for mode, threads in configs:
        arms = [r for r in results if r.mode == mode and r.threads == threads]
        label = arms[0].label

        def mean(values: Sequence[float]) -> float:
            return statistics.fmean(values)

        row = {
            "steady_s_per_img": mean([r.steady_seconds_per_image for r in arms]),
            "encode_ms": mean([r.encode_ms for r in arms]),
            "probe_ms": mean([r.probe_ms_mean for r in arms]),
            "miss_percent": mean([r.probe_miss_percent for r in arms]),
            "waiting_ms": mean([r.waiting_ms for r in arms]),
            "mhz": mean([r.counters.get("mhz", 0.0) for r in arms]),
            "defender": mean([r.counters.get("defender", 0.0) for r in arms]),
        }
        summary[label] = row
        print(
            f"  {label:<18} {row['steady_s_per_img']:.3f} s/img  encode {row['encode_ms']:5.0f} ms"
            f"  probe {row['probe_ms']:5.1f} ms ({row['miss_percent']:4.1f}% miss)"
            f"  wait {row['waiting_ms']:4.0f} ms  {row['mhz']:5.0f} MHz  defender {row['defender']:4.1f}%"
        )

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {"arms": [asdict(r) for r in results], "pooled": summary,
                 "window": args.window, "lookahead": lookahead, "seed": args.seed},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
