"""Does handing the bytes already read to CLIP and the thumbnail remove the cache risk?

`diagnose_overlap_slowdown.py` (2026-10-27) found why an overlapped index
loses part of its gain: the reader thread reads each file through for
SHA-256, and by the time CLIP -- and the thumbnail after it -- open the same
file, Windows has sometimes evicted its pages. With 64 files ahead 15-30% of
files were read from the external disk a second time, ~180 ms each; with 16
ahead almost none were, but that is a margin, not a guarantee, on a machine
with 400-700 MB of standby cache.

This keeps the bytes the reader read and decodes from them instead of from
the path: one read of the file from the disk, whatever the cache does.

**Not what RFC-030 section 7.2 refused.** That was handing CLIP's *decoded
picture* back through `EmbeddingModelPort` so a thumbnail could be cut from
it. Here each consumer still decodes for itself; only where the encoded bytes
come from changes. In this script that is done without touching production
code: the model instance gets a `_preprocess` that opens `BytesIO`, and the
thumbnail generator is a subclass doing the same, both looking the bytes up
by image id in a store the reader fills.

Arms, all with thumbnails (what production does), window = lookahead:

    serial                   today's pipeline
    cache   / lookahead 16   reader thread, consumers re-open the path
    bytes   / lookahead 16   reader thread, consumers decode the bytes
    bytes   / lookahead 64   the lookahead that lost files to the cache,
                             now holding ~0.5 GB in the process instead

Before measuring, a handful of files are encoded from the path and from the
bytes and the vectors compared, because a different decode would be a
different index.

Run it from the repository root:

    .venv/Scripts/python.exe \
        experiments/thumbnails-in-indexing/measure_prefetch_bytes.py --root E:/
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
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
import types
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from diagnose_overlap_slowdown import COUNTERS, CounterSampler  # noqa: F401
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

from PIL import Image as PILImage
from PIL import ImageOps

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.filesystem_image_provider import (
    discovery_sort_key,
)
from app.infrastructure.filesystem.sha256_content_hasher import READ_CHUNK_BYTES
from app.infrastructure.filesystem.thumbnail_generator import (
    JPEG_QUALITY,
    THUMBNAIL_FORMAT,
    PillowThumbnailGenerator,
    _flatten,
    _to_eight_bit,
)
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
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


class ByteStore:
    """Encoded file bytes by image id, from the reader until the thumbnail is cut.

    The model only looks (`get`): the pipeline's per-image retry may
    encode the same image twice. The thumbnail, the last consumer, takes
    (`pop`), which is what bounds the memory to the files in flight.
    """

    def __init__(self) -> None:
        self._bytes: dict[ImageId, bytes] = {}
        self._lock = threading.Lock()
        self.peak_bytes = 0
        self._held = 0

    def put(self, image_id: ImageId, data: bytes) -> None:
        with self._lock:
            self._bytes[image_id] = data
            self._held += len(data)
            self.peak_bytes = max(self.peak_bytes, self._held)

    def get(self, image_id: ImageId) -> bytes | None:
        with self._lock:
            return self._bytes.get(image_id)

    def pop(self, image_id: ImageId) -> bytes | None:
        with self._lock:
            data = self._bytes.pop(image_id, None)
            if data is not None:
                self._held -= len(data)
            return data


class BytesReadAhead:
    """`PrefetchedCandidates`, keeping the bytes it read for the hash.

    Same layout on the reader thread -- `stat`, the EXIF header, then the
    full read -- so the only difference from the cache arm is whether the
    bytes are kept.
    """

    def __init__(
        self,
        provider: SampledFiles,
        device: object,
        mount_point: Path,
        undiscoverable: list[IndexingFailure],
        lookahead: int,
        store: ByteStore,
    ) -> None:
        self._provider = provider
        self._device = device
        self._mount_point = mount_point
        self._undiscoverable = undiscoverable
        self._queue: queue.Queue[object] = queue.Queue(maxsize=lookahead)
        self._store = store
        self._error: BaseException | None = None
        self.digests: dict[ImageId, str | BaseException] = {}
        self.hashing_seconds = 0.0
        self.waiting_seconds = 0.0

    def _read_ahead(self) -> None:
        try:
            for candidate in discovered_candidates(
                self._provider,  # type: ignore[arg-type]
                self._device,  # type: ignore[arg-type]
                self._mount_point,
                self._undiscoverable,
            ):
                started = time.perf_counter()
                try:
                    buffer = bytearray()
                    digest = hashlib.sha256()
                    with candidate.image.require_absolute_path().value.open("rb") as stream:
                        while chunk := stream.read(READ_CHUNK_BYTES):
                            digest.update(chunk)
                            buffer += chunk
                    self._store.put(candidate.image.id, bytes(buffer))
                    result: str | BaseException = digest.hexdigest()
                except Exception as exc:
                    result = exc
                self.hashing_seconds += time.perf_counter() - started
                self.digests[candidate.image.id] = result
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


def bytes_preprocess(store: ByteStore):  # type: ignore[no-untyped-def]
    """`ClipEmbeddingModel._preprocess`, opening the stored bytes when there are any."""

    def _preprocess(self, processor, image: Image):  # type: ignore[no-untyped-def]
        data = store.get(image.id)
        source = io.BytesIO(data) if data is not None else image.require_absolute_path().value
        with PILImage.open(source) as opened:
            pixels = opened.convert("RGB")
            inputs = processor(images=pixels, return_tensors="pt")
        return inputs["pixel_values"]

    return _preprocess


class BytesThumbnailGenerator(PillowThumbnailGenerator):
    """`PillowThumbnailGenerator.generate`, step for step, from the stored bytes."""

    def __init__(self, store: ByteStore) -> None:
        self._store = store

    def generate(self, image: Image, max_edge: int) -> bytes:
        data = self._store.pop(image.id)
        if data is None:
            return super().generate(image, max_edge)
        with PILImage.open(io.BytesIO(data)) as opened:
            opened.thumbnail((max_edge, max_edge))
            oriented = ImageOps.exif_transpose(opened)
            picture = _flatten(_to_eight_bit(oriented))
            buffer = io.BytesIO()
            picture.save(buffer, format=THUMBNAIL_FORMAT, quality=JPEG_QUALITY)
        return buffer.getvalue()


class TimelineClock(SteadyStateClock):
    """`SteadyStateClock`, also keeping when each batch landed and how far the run was."""

    def __init__(self) -> None:
        super().__init__()
        self.marks: list[tuple[float, float, int]] = []

    def batch_persisted(self, progress, durable_through) -> None:  # type: ignore[no-untyped-def]
        self.marks.append(
            (
                time.perf_counter(),
                time.time(),
                progress.processed_images + progress.skipped_images + progress.failed_images,
            )
        )


def segments(
    clock: TimelineClock, every: int, sampler: CounterSampler | None
) -> list[dict[str, float]]:
    """Seconds per image and mean available memory over each `every` settled files."""
    out: list[dict[str, float]] = []
    start = None
    for perf, wall, settled in clock.marks:
        if start is None:
            start = (perf, wall, settled)
            continue
        if settled - start[2] >= every:
            counters = sampler.averages(start[1], wall) if sampler is not None else {}
            out.append(
                {
                    "until": float(settled),
                    "seconds_per_image": (perf - start[0]) / (settled - start[2]),
                    "available_mb": counters.get("available_mb", 0.0),
                    "mhz": counters.get("mhz", 0.0),
                }
            )
            start = (perf, wall, settled)
    return out


@dataclass
class ArmResult:
    arm: int
    mode: str
    lookahead: int
    files: int
    failed: int
    thumbnail_failures: int
    megabytes: float
    elapsed_seconds: float
    steady_seconds_per_image: float
    discovery_ms: float
    hashing_ms: float
    waiting_ms: float
    inference_ms: float
    thumbnail_ms: float
    held_peak_mb: float
    counters: dict[str, float] = field(default_factory=dict)
    timeline: list[dict[str, float]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.mode if self.mode == "serial" else f"{self.mode}/L{self.lookahead}"


def run_arm(
    arm: int,
    mode: str,
    lookahead: int,
    paths: list[Path],
    device: object,
    mount_point: Path,
    model: object,
    scratch: Path,
    sampler: CounterSampler | None,
) -> ArmResult:
    provider = SampledFiles(paths)
    undiscoverable: list[IndexingFailure] = []
    store = ByteStore()
    original_preprocess = model._preprocess  # type: ignore[attr-defined]
    generator: PillowThumbnailGenerator = PillowThumbnailGenerator()
    waiting = lambda: 0.0  # noqa: E731

    if mode == "serial":
        timed = TimedHasher()
        hasher: object = timed
        hashing = lambda: timed.seconds  # noqa: E731
        candidates: object = discovered_candidates(
            provider, device, mount_point, undiscoverable  # type: ignore[arg-type]
        )
    elif mode == "cache":
        cached = PrefetchedCandidates(provider, device, mount_point, undiscoverable, lookahead)
        hasher = PrecomputedHasher(cached.digests)
        hashing = lambda: cached.hashing_seconds  # noqa: E731
        waiting = lambda: cached.waiting_seconds  # noqa: E731
        candidates = cached
    elif mode == "bytes":
        held = BytesReadAhead(provider, device, mount_point, undiscoverable, lookahead, store)
        hasher = PrecomputedHasher(held.digests)
        hashing = lambda: held.hashing_seconds  # noqa: E731
        waiting = lambda: held.waiting_seconds  # noqa: E731
        candidates = held
        model._preprocess = types.MethodType(bytes_preprocess(store), model)  # type: ignore[attr-defined]
        generator = BytesThumbnailGenerator(store)
    else:
        raise ValueError(mode)

    use_case = IndexOrUpdateImagesUseCase(
        repository=InMemoryImageRepository(),
        embedding_model=model,  # type: ignore[arg-type]
        content_hasher=hasher,  # type: ignore[arg-type]
        batch_size=settings.batch_size,
        metadata_prefetch_size=lookahead,
        thumbnail_writer=ThumbnailWriter(
            generator=generator,
            store=FilesystemThumbnailStore(scratch / f"arm{arm}"),
            max_edge=settings.thumbnail_max_edge,
        ),
    )
    clock = TimelineClock()
    gc.collect()
    wall_start = time.time()
    started = time.perf_counter()
    try:
        summary = use_case.execute(candidates, observer=clock)  # type: ignore[arg-type]
    finally:
        model._preprocess = original_preprocess  # type: ignore[attr-defined]
    finished = time.perf_counter()
    wall_end = time.time()

    settled = summary.indexed + summary.failed
    if clock.first_window_at is not None and settled > clock.settled_at_first_window:
        steady = (finished - clock.first_window_at) / (settled - clock.settled_at_first_window)
    else:
        steady = (finished - started) / len(paths)

    per_image = 1000.0 / len(paths)
    return ArmResult(
        arm=arm,
        mode=mode,
        lookahead=lookahead,
        files=len(paths),
        failed=summary.failed + len(undiscoverable),
        thumbnail_failures=len(summary.thumbnail_failures),
        megabytes=provider.bytes / MEGABYTE,
        elapsed_seconds=summary.elapsed_seconds,
        steady_seconds_per_image=steady,
        discovery_ms=provider.seconds * per_image,
        hashing_ms=hashing() * per_image,
        waiting_ms=waiting() * per_image,
        inference_ms=summary.inference_seconds * per_image,
        thumbnail_ms=summary.thumbnail_seconds * per_image,
        held_peak_mb=store.peak_bytes / MEGABYTE,
        counters=sampler.averages(wall_start, wall_end) if sampler is not None else {},
        timeline=segments(clock, SEGMENT, sampler),
    )


SEGMENT = 150
"""Files per timeline segment; set from `--segment`."""


def check_equivalence(model: object, paths: list[Path], device: object, mount_point: Path) -> float:
    """Largest component difference between path-decoded and bytes-decoded vectors."""
    images = [
        candidate.image
        for candidate in discovered_candidates(
            SampledFiles(paths), device, mount_point, []  # type: ignore[arg-type]
        )
    ]
    from_path = model.encode_images(images)  # type: ignore[attr-defined]
    store = ByteStore()
    for image in images:
        store.put(image.id, image.require_absolute_path().value.read_bytes())
    original = model._preprocess  # type: ignore[attr-defined]
    model._preprocess = types.MethodType(bytes_preprocess(store), model)  # type: ignore[attr-defined]
    try:
        from_bytes = model.encode_images(images)  # type: ignore[attr-defined]
    finally:
        model._preprocess = original  # type: ignore[attr-defined]
    path_thumbs = [PillowThumbnailGenerator().generate(i, settings.thumbnail_max_edge) for i in images]
    bytes_thumbs = [BytesThumbnailGenerator(store).generate(i, settings.thumbnail_max_edge) for i in images]
    identical_thumbs = sum(a == b for a, b in zip(path_thumbs, bytes_thumbs, strict=True))
    delta = max(
        abs(a - b)
        for left, right in zip(from_path, from_bytes, strict=True)
        for a, b in zip(left.values, right.values, strict=True)
    )
    print(
        f"equivalence on {len(images)} files: max |delta| between vectors {delta:.3e}, "
        f"thumbnails byte-identical {identical_thumbs}/{len(images)}"
    )
    return delta


def print_arm(result: ArmResult) -> None:
    c = result.counters
    print(
        f"  {result.arm:>3}  {result.label:<12} {1 / result.steady_seconds_per_image:>6.2f}"
        f"  {result.discovery_ms:>5.0f} {result.hashing_ms:>5.0f} {result.waiting_ms:>5.0f}"
        f" {result.inference_ms:>6.0f} {result.thumbnail_ms:>6.0f}"
        f"  {result.held_peak_mb:>6.0f}"
        f"  {c.get('mhz', 0):>5.0f} {c.get('available_mb', 0):>6.0f} {c.get('standby', 0) / MEGABYTE:>6.0f}"
        f"  {result.failed:>3} {result.thumbnail_failures:>3}"
    )
    if len(result.timeline) > 1:
        print(
            "        timeline s/img (availMB) per "
            f"{SEGMENT} files: "
            + "  ".join(
                f"{s['seconds_per_image']:.3f} ({s['available_mb']:.0f})"
                for s in result.timeline
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--per-arm", type=int, default=192)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--warm-up", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20261031)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--configs",
        default="serial:16,cache:16,bytes:16,bytes:64",
        help="mode:lookahead pairs, alternated in this order every round",
    )
    parser.add_argument("--segment", type=int, default=150)
    args = parser.parse_args()
    global SEGMENT
    SEGMENT = args.segment

    root = args.root.resolve()
    print(f"python {platform.python_version()}, {platform.platform()}")
    print(f"processor: {platform.processor()}, {os.cpu_count()} logical CPUs")
    print(f"batch_size {settings.batch_size}, thumbnails on, window = lookahead per arm")

    listed = list_supported(root)
    configs = [
        (mode, int(lookahead))
        for mode, lookahead in (pair.split(":") for pair in args.configs.split(","))
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
    delta = check_equivalence(model, warm_up[:8], device, volume.mount_point)

    scratch = Path(tempfile.mkdtemp(prefix="solidvision-bytes-experiment-"))
    results: list[ArmResult] = []
    try:
        with CounterSampler(scratch / "counters.csv") as sampler:
            warm = run_arm(-1, "bytes", 16, warm_up, device, volume.mount_point, model, scratch, None)
            print(f"warm-up: {warm.files} files in {warm.elapsed_seconds:.1f} s (not measured)")
            print()
            print("  arm  config        steady  ---------- ms per image ----------   MB peak  -- counters (mean) --  fail")
            print("                     img/s    disc  hash  wait  infer  thumb     held    MHz  availMB standbyMB  idx thm")
            for arm, ((mode, lookahead), paths) in enumerate(zip(plan, groups, strict=True)):
                result = run_arm(arm, mode, lookahead, paths, device, volume.mount_point,
                                 model, scratch, sampler)
                results.append(result)
                print_arm(result)
                sys.stdout.flush()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    print("pooled steady state")
    pooled: dict[str, float] = {}
    for mode, lookahead in configs:
        arms = [r for r in results if r.mode == mode and r.lookahead == lookahead]
        seconds = statistics.fmean(r.steady_seconds_per_image for r in arms)
        pooled[arms[0].label] = seconds
        spread = [r.steady_seconds_per_image for r in arms]
        print(
            f"  {arms[0].label:<12} {seconds:.3f} s/img  {1 / seconds:5.2f} img/s  "
            f"(arms {min(spread):.3f}-{max(spread):.3f})  infer "
            f"{statistics.fmean(r.inference_ms for r in arms):4.0f} ms  thumb "
            f"{statistics.fmean(r.thumbnail_ms for r in arms):3.0f} ms  "
            f"-> {seconds * len(listed) / 3600:5.1f} h for {len(listed)} files"
        )
    baseline = pooled.get("serial")
    for label, seconds in pooled.items():
        if baseline is not None and label != "serial":
            print(f"  {label} vs serial: {100 * (seconds - baseline) / baseline:+.1f}%")

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {"arms": [asdict(r) for r in results], "pooled": pooled,
                 "equivalence_max_delta": delta, "seed": args.seed},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
