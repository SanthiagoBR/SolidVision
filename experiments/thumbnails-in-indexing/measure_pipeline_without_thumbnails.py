"""Does dropping thumbnails from indexing make a cold index of the real archive faster?

RFC-030 section 7.2 renders a thumbnail for every image the pipeline embeds,
between inference and persistence. Two earlier measurements bracket its cost
and neither answers the question for the real collection:

* `experiments/rfc-030-file-access` and `rfc-040-on-demand-thumbnails` ran
  the pipeline with and without a `ThumbnailWriter`, but over the demo corpus
  and synthetic upscales, **all in the OS cache** -- and on 45 files the
  run-to-run noise (5.25 s vs 7.19 s for the same arm) was as large as the
  effect;
* the 400-file sample of `E:` (2026-10-11) timed the thumbnail stage **in
  isolation**, cold: 0.160 s/img, 24% of the per-image cost. In the pipeline
  the file has already been read in full by SHA-256 before CLIP decodes it,
  so the thumbnail decodes from cache and the isolated figure double-counts
  the disk.

This script runs the whole pipeline over real files from the archive, read
cold. The sample is drawn from the entire root, sorted in discovery order and
dealt round-robin into arms that alternate without / with thumbnails
(`--rounds 2` gives off, on, off, on). Every file belongs to exactly one arm,
so every arm pays its own disk reads, and alternating arms lets drift show up
instead of hiding inside the difference.

What runs is what `build_runner()` composes, except two things, both equal
across arms: the repository is in memory (persistence was 2.1% of elapsed in
RFC-024 section 7.2) and the thumbnail store is a temporary directory, not the
real cache. Discovery is the per-file body of
`FilesystemImageProvider._discover_under()` -- `stat` plus one EXIF read --
over the sampled list instead of a walk. Nothing is written to the archive.

Run it from the repository root:

    .venv/Scripts/python.exe \
        experiments/thumbnails-in-indexing/measure_pipeline_without_thumbnails.py \
        --root E:/
"""

from __future__ import annotations

import argparse
import datetime
import gc
import json
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPOSITORY_ROOT), str(REPOSITORY_ROOT / "backend")]

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexingFailure,
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.thumbnail_writer import ThumbnailWriter  # noqa: E402
from app.domain.entities.image import Image  # noqa: E402
from app.domain.services.content_hasher_port import ContentHasherPort  # noqa: E402
from app.infrastructure.config.settings import settings  # noqa: E402
from app.infrastructure.filesystem.discovered_image_file import (  # noqa: E402
    DiscoveredImageFile,
)
from app.infrastructure.filesystem.exif_capture_date import (  # noqa: E402
    read_exif_facts,
)
from app.infrastructure.filesystem.filesystem_image_provider import (  # noqa: E402
    discovery_sort_key,
)
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.filesystem.thumbnail_generator import (  # noqa: E402
    PillowThumbnailGenerator,
)
from app.infrastructure.filesystem.thumbnail_store import (  # noqa: E402
    FilesystemThumbnailStore,
)
from app.infrastructure.filesystem.volume_identity_provider import (  # noqa: E402
    WindowsVolumeIdentityProvider,
)
from app.infrastructure.persistence.in_memory_device_repository import (  # noqa: E402
    InMemoryDeviceRepository,
)
from app.infrastructure.persistence.in_memory_image_repository import (  # noqa: E402
    InMemoryImageRepository,
)
from app.infrastructure.workers.indexing_worker import (  # noqa: E402
    discovered_candidates,
    register_device,
)
from app.presentation.dependencies import get_embedding_model  # noqa: E402

MEGABYTE = 1024 * 1024


class SampledFiles:
    """A provider over a fixed list of files, timing the per-file discovery work.

    Duck-typed for `discovered_candidates()`, which only calls `discover()`.
    The body is `_discover_under()`'s, minus the walk and the checkpoint,
    so the cost of the EXIF read stays inside the run as it does in a job.
    """

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        self.seconds = 0.0
        self.bytes = 0

    def discover(self) -> Iterator[DiscoveredImageFile]:
        for path in self._paths:
            started = time.perf_counter()
            stat = path.stat()
            facts = read_exif_facts(
                path,
                capture_date=settings.extract_capture_date,
                position=settings.extract_gps,
            )
            discovered = DiscoveredImageFile(
                path=path,
                filename=path.stem,
                extension=path.suffix.lower().lstrip("."),
                file_size=stat.st_size,
                file_modified_at=datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ),
                capture_date=facts.capture_date,
                position=facts.position,
            )
            self.seconds += time.perf_counter() - started
            self.bytes += stat.st_size
            yield discovered


class TimedHasher(ContentHasherPort):
    """SHA-256, timed: for a new file it is the full read from the disk."""

    def __init__(self) -> None:
        self._inner = Sha256ContentHasher()
        self.seconds = 0.0

    def hash_image(self, image: Image) -> str:
        started = time.perf_counter()
        try:
            return self._inner.hash_image(image)
        finally:
            self.seconds += time.perf_counter() - started


@dataclass
class ArmResult:
    """One arm of the experiment, with the pipeline's own stage timings."""

    arm: int
    thumbnails: bool
    files: int
    indexed: int
    failed: int
    thumbnail_failures: int
    megabytes: float
    elapsed_seconds: float
    discovery_seconds: float
    hashing_seconds: float
    inference_seconds: float
    thumbnail_seconds: float
    persistence_seconds: float
    other_seconds: float

    @property
    def seconds_per_image(self) -> float:
        return self.elapsed_seconds / self.files


def list_supported(root: Path) -> list[Path]:
    """Every supported file under `root`, by directory listing only.

    `os.walk` rather than `rglob` + `is_file()`: the listing reads
    directory entries and never a file's contents, so nothing sampled
    later is in the cache because of it.
    """
    extensions = {ext.lower() for ext in settings.supported_extensions}
    found: list[Path] = []
    for directory, _, filenames in os.walk(root, onerror=lambda error: None):
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() in extensions:
                found.append(Path(directory) / filename)
    return found


def run_arm(
    arm: int,
    paths: list[Path],
    device: object,
    mount_point: Path,
    model: object,
    thumbnail_directory: Path | None,
) -> tuple[ArmResult, IndexingSummary]:
    provider = SampledFiles(paths)
    hasher = TimedHasher()
    writer = (
        ThumbnailWriter(
            generator=PillowThumbnailGenerator(),
            store=FilesystemThumbnailStore(thumbnail_directory),
            max_edge=settings.thumbnail_max_edge,
        )
        if thumbnail_directory is not None
        else None
    )
    use_case = IndexOrUpdateImagesUseCase(
        repository=InMemoryImageRepository(),
        embedding_model=model,  # type: ignore[arg-type]
        content_hasher=hasher,
        batch_size=settings.batch_size,
        metadata_prefetch_size=settings.metadata_prefetch_size,
        thumbnail_writer=writer,
    )
    undiscoverable: list[IndexingFailure] = []
    gc.collect()
    summary = use_case.execute(
        discovered_candidates(provider, device, mount_point, undiscoverable)  # type: ignore[arg-type]
    )
    accounted = (
        provider.seconds
        + hasher.seconds
        + summary.inference_seconds
        + summary.thumbnail_seconds
        + summary.persistence_seconds
    )
    result = ArmResult(
        arm=arm,
        thumbnails=writer is not None,
        files=len(paths),
        indexed=summary.indexed,
        failed=summary.failed + len(undiscoverable),
        thumbnail_failures=len(summary.thumbnail_failures),
        megabytes=provider.bytes / MEGABYTE,
        elapsed_seconds=summary.elapsed_seconds,
        discovery_seconds=provider.seconds,
        hashing_seconds=hasher.seconds,
        inference_seconds=summary.inference_seconds,
        thumbnail_seconds=summary.thumbnail_seconds,
        persistence_seconds=summary.persistence_seconds,
        other_seconds=summary.elapsed_seconds - accounted,
    )
    return result, summary


def print_arm(result: ArmResult) -> None:
    label = "with   " if result.thumbnails else "without"
    per_image = 1000.0 / result.files
    print(
        f"  {result.arm:>3}  {label}  {result.files:>5} {result.megabytes / result.files:>6.2f}"
        f"  {result.elapsed_seconds:>8.1f} {result.files / result.elapsed_seconds:>6.2f}"
        f"  {result.discovery_seconds * per_image:>6.0f} {result.hashing_seconds * per_image:>6.0f}"
        f" {result.inference_seconds * per_image:>6.0f} {result.thumbnail_seconds * per_image:>6.0f}"
        f" {result.persistence_seconds * per_image:>6.1f} {result.other_seconds * per_image:>6.1f}"
        f"  {result.failed:>3} {result.thumbnail_failures:>3}"
    )


def per_image(results: list[ArmResult], field: str) -> float:
    """Seconds per image for one stage, pooled over arms (weighted by files)."""
    files = sum(result.files for result in results)
    return sum(getattr(result, field) for result in results) / files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--per-arm", type=int, default=150)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--warm-up", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    root = args.root.resolve()
    print(f"python {platform.python_version()}, {platform.platform()}")
    print(f"processor: {platform.processor()}, {os.cpu_count()} logical CPUs")
    print(
        f"batch_size {settings.batch_size}, metadata_prefetch_size "
        f"{settings.metadata_prefetch_size}, thumbnail_max_edge "
        f"{settings.thumbnail_max_edge}, extract_capture_date "
        f"{settings.extract_capture_date}, extract_gps {settings.extract_gps}"
    )

    started = time.perf_counter()
    listed = list_supported(root)
    print(
        f"listed {len(listed)} supported files under {root} in "
        f"{time.perf_counter() - started:.1f} s (directory entries only)"
    )

    arms = 2 * args.rounds
    wanted = args.warm_up + arms * args.per_arm
    if wanted > len(listed):
        raise SystemExit(f"asked for {wanted} files, only {len(listed)} exist")
    sample = random.Random(args.seed).sample(listed, wanted)
    warm_up, measured = sample[: args.warm_up], sample[args.warm_up :]
    measured.sort(key=lambda path: discovery_sort_key(path.relative_to(root)))
    groups = [measured[arm::arms] for arm in range(arms)]
    folders = len({path.parent for path in measured})
    print(
        f"sample: {args.warm_up} warm-up + {arms} arms x {args.per_arm} files "
        f"from {folders} folders (seed {args.seed}), arms alternate "
        "without / with thumbnails"
    )

    device, volume = register_device(
        volume_provider=WindowsVolumeIdentityProvider(),
        device_repository=InMemoryDeviceRepository(),
        root=root,
        label="",
        persist=False,
    )

    model = get_embedding_model()
    started = time.perf_counter()
    model.warm_up()
    print(f"model loaded in {time.perf_counter() - started:.1f} s (not measured)")

    scratch = Path(tempfile.mkdtemp(prefix="solidvision-thumbs-experiment-"))
    results: list[ArmResult] = []
    try:
        warm, _ = run_arm(-1, warm_up, device, volume.mount_point, model, scratch / "w")
        print(f"warm-up pass: {warm.files} files in {warm.elapsed_seconds:.1f} s (not measured)")
        print()
        print("  arm  thumbs   files  MB/img   elapsed  img/s   ---- ms per image ------------------------  fail")
        print("                                  (s)            disc   hash  infer  thumb    db  other   idx thm")
        for arm, paths in enumerate(groups):
            thumbnails = arm % 2 == 1
            result, _ = run_arm(
                arm,
                paths,
                device,
                volume.mount_point,
                model,
                scratch / f"arm{arm}" if thumbnails else None,
            )
            results.append(result)
            print_arm(result)
            sys.stdout.flush()
        stored = [
            path.stat().st_size
            for path in scratch.glob("arm*/**/*")
            if path.is_file()
        ]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    without = [result for result in results if not result.thumbnails]
    with_thumbs = [result for result in results if result.thumbnails]
    off = per_image(without, "elapsed_seconds")
    on = per_image(with_thumbs, "elapsed_seconds")
    thumb = per_image(with_thumbs, "thumbnail_seconds")
    on_minus_thumb = on - thumb

    print()
    print("pooled, seconds per image")
    print(f"  without thumbnails:               {off:.3f} s  ({1 / off:.2f} img/s)")
    print(f"  with thumbnails:                  {on:.3f} s  ({1 / on:.2f} img/s)")
    print(f"  difference between arms:          {on - off:+.3f} s  ({100 * (on - off) / off:+.1f}%)")
    print(
        f"  thumbnail stage inside 'with':    {thumb:.3f} s  "
        f"({100 * thumb / on:.1f}% of that arm's elapsed)"
    )
    print(
        f"  'with' minus its thumbnail stage: {on_minus_thumb:.3f} s  "
        "(should sit near 'without' if the stage timing is the whole cost)"
    )
    spread = [result.seconds_per_image for result in without]
    if len(spread) > 1:
        print(
            f"  noise: 'without' arms range {min(spread):.3f}-{max(spread):.3f} s/img "
            f"(stdev {statistics.stdev(spread):.3f})"
        )
    if stored:
        print(
            f"  stored thumbnails: {len(stored)}, mean "
            f"{statistics.fmean(stored) / 1024:.1f} KB"
        )

    total = len(listed)
    print()
    print(f"projection onto the {total} files under {root}")
    for label, seconds in (
        ("without thumbnails", off),
        ("with thumbnails", on),
    ):
        print(f"  {label:<20} {seconds * total / 3600:6.1f} h")
    print(
        f"  thumbnail stage alone {thumb * total / 3600:6.1f} h  "
        "(what a deferred or on-demand thumbnail would move out of the index)"
    )

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "root": str(root),
                    "listed": total,
                    "seed": args.seed,
                    "arms": [asdict(result) for result in results],
                    "pooled_seconds_per_image": {
                        "without": off,
                        "with": on,
                        "thumbnail_stage": thumb,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
