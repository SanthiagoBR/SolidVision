"""Does reading the next files while CLIP encodes the current batch shorten a cold index?

`measure_pipeline_without_thumbnails.py` (2026-10-27) broke a cold index of
the real archive into per-image stages, all of them run one after another on
one thread:

    discovery + EXIF   110 ms   disk
    SHA-256            164 ms   disk -- for a new file, the full read
    inference          192 ms   CPU
    thumbnail           76 ms   CPU, from the OS cache

The disk waits while the CPU works and the CPU waits while the disk works.
This script moves discovery and hashing onto a thread of their own, a bounded
number of files ahead, and leaves `IndexOrUpdateImagesUseCase` exactly as it
is: the main thread consumes the candidates as before, and the hasher it is
given hands back the digest the reader thread already computed. If the two
halves overlap, the per-image cost drops from their sum towards the larger of
the two.

**The window has to shrink with the lookahead, and the RAM is why.** The use
case pulls `metadata_prefetch_size` candidates before it decides any of them.
A reader `L` files ahead of a window of `W` leaves `W - L` reads to happen
serially at every window boundary, so overlapping fully needs `L >= W` -- and
with the production window of 512 that is ~2 GB of photos waiting in the OS
cache for CLIP to open them, on a machine with 7.7 GB. Pages evicted before
CLIP reaches them are read from the disk twice. So every arm here runs with
`--window` (default 64) and the reader `--lookahead` (default: the window)
files ahead, ~250 MB. In the serial arms the window changes nothing: with the
in-memory repository it only decides how many metadata lookups are made.

**Cold index only.** The reader hashes every file because, for a file with no
row, the plan hashes every file. On a re-scan the plan hashes almost nothing,
and a reader that hashed ahead regardless would read the whole disk to skip
it; a production design has to plan before it reads.

Arms rotate serial+thumbnails (what runs today), prefetch+thumbnails and
prefetch without thumbnails, one disjoint sample each, read cold. Throughput
is reported for the whole arm and for the steady state after the first
window, which never overlaps with anything and is noise on 116,000 images.

Run it from the repository root:

    .venv/Scripts/python.exe \
        experiments/thumbnails-in-indexing/measure_prefetch_overlap.py --root E:/
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import queue
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from measure_pipeline_without_thumbnails import (
    MEGABYTE,
    SampledFiles,
    TimedHasher,
    list_supported,
)

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.image import Image
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.services.indexing_observer import IndexingObserver
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.filesystem_image_provider import (
    discovery_sort_key,
)
from app.infrastructure.filesystem.thumbnail_generator import (
    PillowThumbnailGenerator,
)
from app.infrastructure.filesystem.thumbnail_store import (
    FilesystemThumbnailStore,
)
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


class PrefetchedCandidates:
    """Discovery and the full read on a reader thread, `lookahead` files ahead.

    The digest is stored before the candidate is queued, so the main
    thread, which only asks for it after taking the candidate off the
    queue, always finds it: the queue's lock orders the two.

    A failure to hash is stored as the exception and raised in the main
    thread by `PrecomputedHasher`, where `plan_indexing()` reports it as it
    reports any other. A failure of the scan itself is re-raised when the
    stream ends, as it would have propagated out of the generator.
    """

    def __init__(
        self,
        provider: SampledFiles,
        device: object,
        mount_point: Path,
        undiscoverable: list[IndexingFailure],
        lookahead: int,
    ) -> None:
        self._provider = provider
        self._device = device
        self._mount_point = mount_point
        self._undiscoverable = undiscoverable
        self._queue: queue.Queue[object] = queue.Queue(maxsize=lookahead)
        self._hasher = TimedHasher()
        self._error: BaseException | None = None
        self.digests: dict[ImageId, str | BaseException] = {}
        self.waiting_seconds = 0.0

    @property
    def hashing_seconds(self) -> float:
        return self._hasher.seconds

    def _read_ahead(self) -> None:
        try:
            for candidate in discovered_candidates(
                self._provider,  # type: ignore[arg-type]
                self._device,  # type: ignore[arg-type]
                self._mount_point,
                self._undiscoverable,
            ):
                try:
                    digest: str | BaseException = self._hasher.hash_image(
                        candidate.image
                    )
                except Exception as exc:
                    digest = exc
                self.digests[candidate.image.id] = digest
                self._queue.put(candidate)
        except BaseException as exc:
            self._error = exc
        finally:
            self._queue.put(_END)

    def __iter__(self) -> Iterator[IndexCandidate]:
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
        if self._error is not None:
            raise self._error


class PrecomputedHasher(ContentHasherPort):
    """The digest the reader thread computed, so the plan reads nothing."""

    def __init__(self, digests: dict[ImageId, str | BaseException]) -> None:
        self._digests = digests

    def hash_image(self, image: Image) -> str:
        digest = self._digests.pop(image.id)
        if isinstance(digest, BaseException):
            raise digest
        return digest


class SteadyStateClock(IndexingObserver):
    """When the first window was decided, and how many files it had settled."""

    def __init__(self) -> None:
        self.first_window_at: float | None = None
        self.settled_at_first_window = 0

    def discovered(self, files_seen: int, complete: bool) -> None:
        return None

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        if self.first_window_at is None:
            self.first_window_at = time.perf_counter()
            self.settled_at_first_window = (
                progress.processed_images
                + progress.skipped_images
                + progress.failed_images
            )

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def should_stop(self) -> bool:
        return False


@dataclass
class ArmResult:
    """One arm, with stage timings per image and the steady-state rate."""

    arm: int
    mode: str
    thumbnails: bool
    files: int
    indexed: int
    failed: int
    megabytes: float
    elapsed_seconds: float
    steady_seconds_per_image: float
    discovery_seconds: float
    hashing_seconds: float
    waiting_seconds: float
    inference_seconds: float
    thumbnail_seconds: float

    @property
    def label(self) -> str:
        return f"{self.mode}+{'thumbs' if self.thumbnails else 'none'}"


def run_arm(
    arm: int,
    mode: str,
    thumbnails: bool,
    paths: list[Path],
    device: object,
    mount_point: Path,
    model: object,
    scratch: Path,
    window: int,
    lookahead: int,
) -> ArmResult:
    provider = SampledFiles(paths)
    undiscoverable: list[IndexingFailure] = []
    prefetched: PrefetchedCandidates | None = None
    if mode == "prefetch":
        prefetched = PrefetchedCandidates(
            provider, device, mount_point, undiscoverable, lookahead
        )
        hasher: ContentHasherPort = PrecomputedHasher(prefetched.digests)
        candidates: object = prefetched
    else:
        hasher = TimedHasher()
        candidates = discovered_candidates(
            provider, device, mount_point, undiscoverable  # type: ignore[arg-type]
        )
    writer = (
        ThumbnailWriter(
            generator=PillowThumbnailGenerator(),
            store=FilesystemThumbnailStore(scratch / f"arm{arm}"),
            max_edge=settings.thumbnail_max_edge,
        )
        if thumbnails
        else None
    )
    use_case = IndexOrUpdateImagesUseCase(
        repository=InMemoryImageRepository(),
        embedding_model=model,  # type: ignore[arg-type]
        content_hasher=hasher,
        batch_size=settings.batch_size,
        metadata_prefetch_size=window,
        thumbnail_writer=writer,
    )
    clock = SteadyStateClock()
    gc.collect()
    started = time.perf_counter()
    summary = use_case.execute(candidates, observer=clock)  # type: ignore[arg-type]
    finished = time.perf_counter()

    settled = summary.indexed + summary.failed
    if clock.first_window_at is not None and settled > clock.settled_at_first_window:
        steady = (finished - clock.first_window_at) / (
            settled - clock.settled_at_first_window
        )
    else:
        steady = (finished - started) / len(paths)

    return ArmResult(
        arm=arm,
        mode=mode,
        thumbnails=thumbnails,
        files=len(paths),
        indexed=summary.indexed,
        failed=summary.failed + len(undiscoverable),
        megabytes=provider.bytes / MEGABYTE,
        elapsed_seconds=summary.elapsed_seconds,
        steady_seconds_per_image=steady,
        discovery_seconds=provider.seconds,
        hashing_seconds=(
            prefetched.hashing_seconds
            if prefetched is not None
            else hasher.seconds  # type: ignore[attr-defined]
        ),
        waiting_seconds=prefetched.waiting_seconds if prefetched is not None else 0.0,
        inference_seconds=summary.inference_seconds,
        thumbnail_seconds=summary.thumbnail_seconds,
    )


def print_arm(result: ArmResult) -> None:
    per_image = 1000.0 / result.files
    wait = (
        f"{result.waiting_seconds * per_image:>6.0f}"
        if result.mode == "prefetch"
        else f"{'-':>6}"
    )
    print(
        f"  {result.arm:>3}  {result.label:<16} {result.files:>5} "
        f"{result.megabytes / result.files:>6.2f}  {result.elapsed_seconds:>7.1f} "
        f"{result.files / result.elapsed_seconds:>6.2f} "
        f"{1 / result.steady_seconds_per_image:>7.2f}"
        f"  {result.discovery_seconds * per_image:>6.0f} "
        f"{result.hashing_seconds * per_image:>6.0f} {wait} "
        f"{result.inference_seconds * per_image:>6.0f} "
        f"{result.thumbnail_seconds * per_image:>6.0f}  {result.failed:>3}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--per-arm", type=int, default=400)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--lookahead", type=int, default=None)
    parser.add_argument("--warm-up", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    lookahead = args.lookahead if args.lookahead is not None else args.window

    root = args.root.resolve()
    print(f"python {platform.python_version()}, {platform.platform()}")
    print(f"processor: {platform.processor()}, {os.cpu_count()} logical CPUs")
    print(
        f"batch_size {settings.batch_size}, window {args.window} "
        f"(production {settings.metadata_prefetch_size}), lookahead {lookahead}, "
        f"thumbnail_max_edge {settings.thumbnail_max_edge}"
    )

    started = time.perf_counter()
    listed = list_supported(root)
    print(
        f"listed {len(listed)} supported files under {root} in "
        f"{time.perf_counter() - started:.1f} s"
    )

    configs = [("serial", True), ("prefetch", True), ("prefetch", False)]
    plan = configs * args.rounds
    wanted = args.warm_up + len(plan) * args.per_arm
    if wanted > len(listed):
        raise SystemExit(f"asked for {wanted} files, only {len(listed)} exist")
    sample = random.Random(args.seed).sample(listed, wanted)
    warm_up, measured = sample[: args.warm_up], sample[args.warm_up :]
    measured.sort(key=lambda path: discovery_sort_key(path.relative_to(root)))
    groups = [measured[arm :: len(plan)] for arm in range(len(plan))]
    print(
        f"sample: {args.warm_up} warm-up + {len(plan)} arms x {args.per_arm} files "
        f"from {len({path.parent for path in measured})} folders (seed {args.seed})"
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

    scratch = Path(tempfile.mkdtemp(prefix="solidvision-prefetch-experiment-"))
    results: list[ArmResult] = []
    try:
        warm = run_arm(
            -1, "prefetch", True, warm_up, device, volume.mount_point, model,
            scratch, args.window, lookahead,
        )
        print(f"warm-up pass: {warm.files} files in {warm.elapsed_seconds:.1f} s (not measured)")
        print()
        print("  arm  config           files MB/img  elapsed  img/s  steady   ---- ms per image ---------------  fail")
        print("                                         (s)    total   img/s   disc   hash   wait  infer  thumb")
        for arm, ((mode, thumbnails), paths) in enumerate(zip(plan, groups, strict=True)):
            result = run_arm(
                arm, mode, thumbnails, paths, device, volume.mount_point, model,
                scratch, args.window, lookahead,
            )
            results.append(result)
            print_arm(result)
            sys.stdout.flush()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    print("pooled steady state (after the first window)")
    pooled: dict[str, float] = {}
    for mode, thumbnails in configs:
        arms = [r for r in results if r.mode == mode and r.thumbnails == thumbnails]
        label = arms[0].label
        seconds = statistics.fmean(r.steady_seconds_per_image for r in arms)
        pooled[label] = seconds
        spread = [r.steady_seconds_per_image for r in arms]
        print(
            f"  {label:<16} {seconds:.3f} s/img  {1 / seconds:5.2f} img/s  "
            f"(arms {min(spread):.3f}-{max(spread):.3f})  "
            f"-> {seconds * len(listed) / 3600:5.1f} h for {len(listed)} files"
        )
    baseline = pooled["serial+thumbs"]
    for label, seconds in pooled.items():
        if label != "serial+thumbs":
            print(
                f"  {label} vs serial+thumbs: {100 * (seconds - baseline) / baseline:+.1f}%"
            )

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "root": str(root),
                    "listed": len(listed),
                    "seed": args.seed,
                    "window": args.window,
                    "lookahead": lookahead,
                    "arms": [asdict(r) for r in results],
                    "pooled_steady_seconds_per_image": pooled,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
