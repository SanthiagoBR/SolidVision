"""Both candidates for the RFC together: bytes handoff + CLIP decoding at 1/2.

Experiment 4 (`measure_prefetch_bytes.py`) measured a reader thread that
keeps the bytes it hashed, with CLIP and the thumbnail decoding from them:
0.304 s/img with 64 files ahead. Experiment 5 (`measure_clip_draft.py`)
measured, on its own, that asking Pillow's JPEG decoder for half the
resolution (`draft`) before CLIP's preprocessing takes that step from 163 to
94 ms per image.

They need not add up. In experiment 4 the reader thread spent ~268 ms per
image (discovery 111 + hash 157) and the consumer ~301 ms (inference 216 +
thumbnail 86): taking ~70 ms off the consumer may just hand the bottleneck
to the disk. So besides the stage times of experiment 4, this reports how
long the consumer waited for the reader after the first window, the wait
that grows when the disk is the bottleneck.

Arms, all with thumbnails (what production does), a 2x2:

    serial              today's pipeline
    serial-draft        today's pipeline, CLIP decodes at 1/2
    bytes / L64         reader thread, consumers decode the bytes (exp. 4)
    bytes-draft / L64   the same, CLIP decoding the bytes at 1/2

The draft follows experiment 5: JPEG and MPO (`JpegImageFile`; DJI photos
open as MPO), and only when the smaller side at 1/2 is still >= 224 px. The
thumbnail is not touched: `thumbnail()` already asks for a draft itself.

Before measuring, a few files are encoded every way and compared: bytes
without draft must equal today (experiment 4), the draft from the path must
equal the draft from the bytes, and the draft's distance from today is
printed. Nothing is written but thumbnails, to a temporary directory removed
at the end; the image and device repositories are in memory.

Run it from the repository root:

    .venv/Scripts/python.exe \
        experiments/thumbnails-in-indexing/measure_bytes_draft.py --root E:/
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import types
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from diagnose_overlap_slowdown import CounterSampler
from measure_pipeline_without_thumbnails import (
    MEGABYTE,
    SampledFiles,
    TimedHasher,
    list_supported,
)
from measure_prefetch_bytes import (
    _END,
    ArmResult,
    ByteStore,
    BytesReadAhead,
    BytesThumbnailGenerator,
    TimelineClock,
    bytes_preprocess,
    segments,
)
from measure_prefetch_overlap import PrecomputedHasher

import torch
from PIL import Image as PILImage
from PIL.JpegImagePlugin import JpegImageFile

from app.application.use_cases.index_or_update_images import (
    IndexingFailure,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.image import Image
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.filesystem_image_provider import (
    discovery_sort_key,
)
from app.infrastructure.filesystem.thumbnail_generator import PillowThumbnailGenerator
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

MIN_SIDE = 224
"""CLIP's input side: no draft that would leave the smaller side below it."""

SEGMENT = 150
"""Files per timeline segment; set from `--segment`."""


class DraftCount:
    """How many images CLIP's preprocessing saw, and how many it decoded at 1/2."""

    def __init__(self) -> None:
        self.seen = 0
        self.drafted = 0


def draft_preprocess(store: ByteStore | None, count: DraftCount):  # type: ignore[no-untyped-def]
    """`ClipEmbeddingModel._preprocess` with `draft` at 1/2, from the stored bytes when there are any."""

    def _preprocess(self, processor, image: Image):  # type: ignore[no-untyped-def]
        data = store.get(image.id) if store is not None else None
        source = io.BytesIO(data) if data is not None else image.require_absolute_path().value
        with PILImage.open(source) as opened:
            count.seen += 1
            if isinstance(opened, JpegImageFile):
                width, height = opened.size
                if min(width // 2, height // 2) >= MIN_SIDE:
                    opened.draft("RGB", (width // 2, height // 2))
                    count.drafted += 1
            pixels = opened.convert("RGB")
            inputs = processor(images=pixels, return_tensors="pt")
        return inputs["pixel_values"]

    return _preprocess


class TimedBytesReadAhead(BytesReadAhead):
    """`BytesReadAhead`, also keeping when each wait for the reader ended."""

    def __init__(
        self,
        provider: SampledFiles,
        device: object,
        mount_point: Path,
        undiscoverable: list[IndexingFailure],
        lookahead: int,
        store: ByteStore,
    ) -> None:
        super().__init__(provider, device, mount_point, undiscoverable, lookahead, store)
        self.waits: list[tuple[float, float]] = []

    def __iter__(self) -> Iterator[IndexCandidate]:
        reader = threading.Thread(target=self._read_ahead, daemon=True)
        reader.start()
        while True:
            started = time.perf_counter()
            item = self._queue.get()
            ended = time.perf_counter()
            self.waiting_seconds += ended - started
            self.waits.append((ended, ended - started))
            if item is _END:
                break
            yield item  # type: ignore[misc]
        reader.join()
        if self._error is not None:
            raise self._error


@dataclass
class DraftArmResult(ArmResult):
    steady_waiting_ms: float = 0.0
    drafted: int = 0
    preprocessed: int = 0

    @property
    def label(self) -> str:
        return self.mode if self.mode.startswith("serial") else f"{self.mode}/L{self.lookahead}"


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
) -> DraftArmResult:
    provider = SampledFiles(paths)
    undiscoverable: list[IndexingFailure] = []
    store = ByteStore()
    count = DraftCount()
    original_preprocess = model._preprocess  # type: ignore[attr-defined]
    generator: PillowThumbnailGenerator = PillowThumbnailGenerator()
    reader: TimedBytesReadAhead | None = None
    waiting = lambda: 0.0  # noqa: E731

    if mode in ("serial", "serial-draft"):
        timed = TimedHasher()
        hasher: object = timed
        hashing = lambda: timed.seconds  # noqa: E731
        candidates: object = discovered_candidates(
            provider, device, mount_point, undiscoverable  # type: ignore[arg-type]
        )
        if mode == "serial-draft":
            model._preprocess = types.MethodType(draft_preprocess(None, count), model)  # type: ignore[attr-defined]
    elif mode in ("bytes", "bytes-draft"):
        reader = TimedBytesReadAhead(provider, device, mount_point, undiscoverable, lookahead, store)
        hasher = PrecomputedHasher(reader.digests)
        hashing = lambda: reader.hashing_seconds  # noqa: E731
        waiting = lambda: reader.waiting_seconds  # noqa: E731
        candidates = reader
        preprocess = (
            draft_preprocess(store, count) if mode == "bytes-draft" else bytes_preprocess(store)
        )
        model._preprocess = types.MethodType(preprocess, model)  # type: ignore[attr-defined]
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
    steady_files = settled - clock.settled_at_first_window
    if clock.first_window_at is not None and steady_files > 0:
        steady = (finished - clock.first_window_at) / steady_files
    else:
        steady = (finished - started) / len(paths)
        steady_files = 0
    steady_waiting = 0.0
    if reader is not None and clock.first_window_at is not None:
        steady_waiting = sum(
            seconds for ended, seconds in reader.waits if ended > clock.first_window_at
        )

    per_image = 1000.0 / len(paths)
    return DraftArmResult(
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
        steady_waiting_ms=1000.0 * steady_waiting / steady_files if steady_files else 0.0,
        drafted=count.drafted,
        preprocessed=count.seen,
    )


def check_equivalence(
    model: object, paths: list[Path], device: object, mount_point: Path
) -> dict[str, float]:
    """Encode a few files every way; bytes must not change a vector, the draft may."""
    images = [
        candidate.image
        for candidate in discovered_candidates(
            SampledFiles(paths), device, mount_point, []  # type: ignore[arg-type]
        )
    ]
    store = ByteStore()
    for image in images:
        store.put(image.id, image.require_absolute_path().value.read_bytes())

    def encode(preprocess) -> torch.Tensor:  # type: ignore[no-untyped-def]
        original = model._preprocess  # type: ignore[attr-defined]
        if preprocess is not None:
            model._preprocess = types.MethodType(preprocess, model)  # type: ignore[attr-defined]
        try:
            vectors = model.encode_images(images)  # type: ignore[attr-defined]
        finally:
            model._preprocess = original  # type: ignore[attr-defined]
        return torch.tensor([list(vector.values) for vector in vectors])

    count = DraftCount()
    today = encode(None)
    from_bytes = encode(bytes_preprocess(store))
    draft_from_path = encode(draft_preprocess(None, DraftCount()))
    draft_from_bytes = encode(draft_preprocess(store, count))
    cosine = (today * draft_from_bytes).sum(dim=1)
    path_thumbs = [PillowThumbnailGenerator().generate(i, settings.thumbnail_max_edge) for i in images]
    bytes_thumbs = [BytesThumbnailGenerator(store).generate(i, settings.thumbnail_max_edge) for i in images]
    identical = sum(a == b for a, b in zip(path_thumbs, bytes_thumbs, strict=True))

    result = {
        "files": len(images),
        "bytes_vs_today_max_delta": float((today - from_bytes).abs().max()),
        "draft_path_vs_draft_bytes_max_delta": float((draft_from_path - draft_from_bytes).abs().max()),
        "draft_vs_today_cosine_mean": float(cosine.mean()),
        "draft_vs_today_cosine_min": float(cosine.min()),
        "drafted": count.drafted,
        "thumbnails_identical": identical,
    }
    print(
        f"equivalence on {len(images)} files: bytes vs today max |delta| "
        f"{result['bytes_vs_today_max_delta']:.3e}; draft from path vs from bytes max |delta| "
        f"{result['draft_path_vs_draft_bytes_max_delta']:.3e}; draft vs today cosine mean "
        f"{result['draft_vs_today_cosine_mean']:.4f} min {result['draft_vs_today_cosine_min']:.4f} "
        f"(drafted {count.drafted}/{len(images)}); thumbnails byte-identical {identical}/{len(images)}"
    )
    return result


def print_arm(result: DraftArmResult) -> None:
    c = result.counters
    print(
        f"  {result.arm:>3}  {result.label:<17} {1 / result.steady_seconds_per_image:>6.2f}"
        f"  {result.discovery_ms:>5.0f} {result.hashing_ms:>5.0f} {result.waiting_ms:>5.0f}"
        f" {result.steady_waiting_ms:>6.0f} {result.inference_ms:>6.0f} {result.thumbnail_ms:>6.0f}"
        f"  {result.held_peak_mb:>6.0f}"
        f"  {c.get('mhz', 0):>5.0f} {c.get('available_mb', 0):>6.0f} {c.get('standby', 0) / MEGABYTE:>6.0f}"
        f"  {result.failed:>3} {result.thumbnail_failures:>3}"
        f"  {result.drafted:>3}/{result.preprocessed}"
    )
    if len(result.timeline) > 1:
        print(
            f"        timeline s/img (availMB) per {SEGMENT} files: "
            + "  ".join(
                f"{s['seconds_per_image']:.3f} ({s['available_mb']:.0f})" for s in result.timeline
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--per-arm", type=int, default=192)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--warm-up", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--configs",
        default="serial:16,serial-draft:16,bytes:64,bytes-draft:64",
        help="mode:lookahead pairs, alternated in this order every round",
    )
    parser.add_argument("--segment", type=int, default=150)
    args = parser.parse_args()
    global SEGMENT
    SEGMENT = args.segment

    root = args.root.resolve()
    print(f"python {platform.python_version()}, {platform.platform()}")
    print(f"processor: {platform.processor()}, {os.cpu_count()} logical CPUs")
    print(f"torch {torch.__version__}, {torch.get_num_threads()} threads")
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
    equivalence = check_equivalence(model, warm_up[:8], device, volume.mount_point)

    scratch = Path(tempfile.mkdtemp(prefix="solidvision-bytes-draft-experiment-"))
    results: list[DraftArmResult] = []
    try:
        with CounterSampler(scratch / "counters.csv") as sampler:
            warm = run_arm(-1, "bytes-draft", 16, warm_up, device, volume.mount_point, model, scratch, None)
            print(f"warm-up: {warm.files} files in {warm.elapsed_seconds:.1f} s (not measured)")
            print()
            print("  arm  config            steady  ------------- ms per image -------------   MB peak  -- counters (mean) --  fail  drafted")
            print("                         img/s    disc  hash  wait  swait  infer  thumb     held    MHz  availMB standbyMB  idx thm")
            for arm, ((mode, lookahead), paths) in enumerate(zip(plan, groups, strict=True)):
                result = run_arm(arm, mode, lookahead, paths, device, volume.mount_point,
                                 model, scratch, sampler)
                results.append(result)
                print_arm(result)
                sys.stdout.flush()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print()
    print("pooled steady state (swait = consumer waiting for the reader after the first window)")
    pooled: dict[str, dict[str, float]] = {}
    for mode, lookahead in configs:
        arms = [r for r in results if r.mode == mode and r.lookahead == lookahead]
        spread = [r.steady_seconds_per_image for r in arms]
        row = {
            "seconds_per_image": statistics.fmean(spread),
            "arm_min": min(spread),
            "arm_max": max(spread),
            "discovery_ms": statistics.fmean(r.discovery_ms for r in arms),
            "hashing_ms": statistics.fmean(r.hashing_ms for r in arms),
            "steady_waiting_ms": statistics.fmean(r.steady_waiting_ms for r in arms),
            "inference_ms": statistics.fmean(r.inference_ms for r in arms),
            "thumbnail_ms": statistics.fmean(r.thumbnail_ms for r in arms),
            "mhz": statistics.fmean(r.counters.get("mhz", 0.0) for r in arms),
        }
        row["hours"] = row["seconds_per_image"] * len(listed) / 3600
        pooled[arms[0].label] = row
        print(
            f"  {arms[0].label:<17} {row['seconds_per_image']:.3f} s/img "
            f"(arms {row['arm_min']:.3f}-{row['arm_max']:.3f})  "
            f"reader {row['discovery_ms'] + row['hashing_ms']:4.0f} ms "
            f"(disc {row['discovery_ms']:3.0f} + hash {row['hashing_ms']:3.0f})  "
            f"consumer {row['inference_ms'] + row['thumbnail_ms']:4.0f} ms "
            f"(infer {row['inference_ms']:3.0f} + thumb {row['thumbnail_ms']:3.0f})  "
            f"swait {row['steady_waiting_ms']:3.0f} ms  {row['mhz']:4.0f} MHz  "
            f"-> {row['hours']:5.1f} h for {len(listed)} files"
        )
    baseline = pooled.get("serial")
    if baseline is not None:
        for label, row in pooled.items():
            if label != "serial":
                change = row["seconds_per_image"] / baseline["seconds_per_image"] - 1
                print(f"  {label} vs serial: {100 * change:+.1f}%")
    for label, row in pooled.items():
        without = pooled.get(label.replace("-draft", ""))
        if "-draft" in label and without is not None:
            change = row["seconds_per_image"] / without["seconds_per_image"] - 1
            print(f"  draft effect, {label} vs {label.replace('-draft', '')}: {100 * change:+.1f}%")

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {"arms": [asdict(r) for r in results], "pooled": pooled,
                 "equivalence": equivalence, "seed": args.seed, "listed": len(listed)},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
