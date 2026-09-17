"""What does a thumbnail cost per image, and how big is it? Measure, do not assume.

RFC-030 section 7.2 as proposed said a 512 px thumbnail would come "from the
image already decoded" for CLIP, at the price of a resize and an encode. In
this codebase it cannot (build-prompt section 4.1): the decoded picture never
leaves `ClipEmbeddingModel._preprocess()`, so `PillowThumbnailGenerator` opens
and decodes the file again. This script measures that real cost -- against
the inference beside it, not against zero -- and the two numbers the RFC left
`TBM`:

    section 7.2   cost per image, and how much it moves the RFC-024
                  throughput (2.2 img/s)
    section 9     average thumbnail size, projected onto 100,000 images

Four parts:

    1. the generator alone over the demo corpus -- 45 real photographs,
       all about 1024 px on their long side;
    2. the generator alone over synthetic 12 MP and 20 MP JPEGs, because
       real camera and drone photos are 4-20x the pixels of the demo corpus
       and a decode is what grows with them. They are demo photos upscaled
       with LANCZOS and saved at quality 90: realistic dimensions and file
       sizes, smoother content than a real sensor produces;
    3. the same large files through a *full* decode first, which is what
       any ordering other than `thumbnail()`-before-`exif_transpose()`
       would pay -- the number behind that ordering's docstring;
    4. `IndexOrUpdateImagesUseCase` end to end with the **real CLIP model**,
       with and without a `ThumbnailWriter`, bracketed (without / with /
       without) so drift between runs shows up rather than hiding inside
       the difference.

**Not measured, and said so in the output:** cold reads from a spinning
external disk. Every file is read once before timing, so the OS cache holds
it; the numbers are decode and encode cost, not seek time.

Run it with the backend virtualenv, from the repository root:

    backend/.venv/Scripts/python.exe \
        experiments/rfc-030-file-access/measure_thumbnail_cost.py

`RFC030_LARGE_FILES` sets how many synthetic files per size (default 20).
`RFC030_SKIP_MODEL=1` skips part 4.
"""

from __future__ import annotations

import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision")
sys.path.insert(0, "C:/Users/chapi/Documents/SolidVision/backend")

import PIL  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from app.application.use_cases.index_or_update_images import (  # noqa: E402
    IndexingSummary,
    IndexOrUpdateImagesUseCase,
)
from app.application.use_cases.indexing_plan import IndexCandidate  # noqa: E402
from app.application.use_cases.thumbnail_writer import ThumbnailWriter  # noqa: E402
from app.domain.entities.image import Image  # noqa: E402
from app.domain.value_objects.device_id import DeviceId  # noqa: E402
from app.domain.value_objects.image_id import ImageId  # noqa: E402
from app.domain.value_objects.image_path import ImagePath  # noqa: E402
from app.infrastructure.filesystem.sha256_content_hasher import (  # noqa: E402
    Sha256ContentHasher,
)
from app.infrastructure.filesystem.thumbnail_generator import (  # noqa: E402
    PillowThumbnailGenerator,
)
from app.infrastructure.filesystem.thumbnail_store import (  # noqa: E402
    FilesystemThumbnailStore,
)
from app.infrastructure.persistence.in_memory_image_repository import (  # noqa: E402
    InMemoryImageRepository,
)

DEMO_CORPUS = Path("C:/Users/chapi/Documents/SolidVision/backend/dataset/demo/images")
MAX_EDGE = 512
REPEATS = 5
LARGE_FILES = int(os.environ.get("RFC030_LARGE_FILES", "20"))
SIZES = {"12 MP (4000x3000)": (4000, 3000), "20 MP (5472x3648)": (5472, 3648)}
DEVICE_ID = DeviceId(uuid.UUID("00000000-0000-0000-0000-000000000030"))
INFERENCE_MS_RFC_024 = 450.0


def image_at(path: Path, root: Path) -> Image:
    relative = ImagePath(path.relative_to(root))
    return Image(
        id=ImageId(uuid.uuid5(uuid.NAMESPACE_URL, str(path))),
        device_id=DEVICE_ID,
        relative_path=relative,
        filename=path.stem,
        extension=path.suffix.lstrip(".").lower(),
        absolute_path=ImagePath(path),
    )


def warm_cache(paths: Sequence[Path]) -> None:
    for path in paths:
        path.read_bytes()


def describe(label: str, samples_ms: Sequence[float]) -> str:
    ordered = sorted(samples_ms)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return (
        f"  {label:<34} median {statistics.median(ordered):8.2f} ms   "
        f"mean {statistics.fmean(ordered):8.2f} ms   p95 {p95:8.2f} ms   "
        f"max {ordered[-1]:8.2f} ms   (n={len(ordered)})"
    )


def time_each(
    paths: Sequence[Path], action: Callable[[Path], object], repeats: int
) -> list[float]:
    samples = []
    for _ in range(repeats):
        for path in paths:
            started = time.perf_counter()
            action(path)
            samples.append((time.perf_counter() - started) * 1000)
    return samples


def naive_full_decode(path: Path) -> bytes:
    """The adapter's cost if anything forced the decode before `thumbnail()`."""
    import io

    from PIL import ImageOps

    with PILImage.open(path) as opened:
        oriented = ImageOps.exif_transpose(opened)
        picture = oriented.convert("RGB")
        picture.thumbnail((MAX_EDGE, MAX_EDGE))
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def build_large_corpus(target: Path, demo: Sequence[Path]) -> dict[str, list[Path]]:
    corpora: dict[str, list[Path]] = {}
    for label, size in SIZES.items():
        folder = target / f"{size[0]}x{size[1]}"
        folder.mkdir(parents=True)
        files = []
        for index in range(LARGE_FILES):
            source = demo[index % len(demo)]
            with PILImage.open(source) as opened:
                upscaled = opened.convert("RGB").resize(
                    size, PILImage.Resampling.LANCZOS
                )
            path = folder / f"large_{index:03d}.jpg"
            upscaled.save(path, "JPEG", quality=90)
            files.append(path)
        corpora[label] = files
    return corpora


def run_pipeline(
    paths: Sequence[Path],
    root: Path,
    model: object,
    writer: ThumbnailWriter | None,
) -> IndexingSummary:
    use_case = IndexOrUpdateImagesUseCase(
        repository=InMemoryImageRepository(),
        embedding_model=model,  # type: ignore[arg-type]
        content_hasher=Sha256ContentHasher(),
        batch_size=8,
        metadata_prefetch_size=512,
        thumbnail_writer=writer,
    )
    candidates = [
        IndexCandidate(image=image_at(path, root), file_size=1, file_modified_at=None)
        for path in paths
    ]
    return use_case.execute(candidates)


def main() -> None:
    print(
        f"python {platform.python_version()}, Pillow {PIL.__version__}, "
        f"{platform.platform()}"
    )
    print(f"processor: {platform.processor()}")
    print(f"max_edge: {MAX_EDGE}, JPEG quality 85 (thumbnail_generator.JPEG_QUALITY)")
    print()

    demo = sorted(p for p in DEMO_CORPUS.rglob("*") if p.is_file())
    generator = PillowThumbnailGenerator()
    workdir = Path(tempfile.mkdtemp(prefix="rfc030-thumbs-"))
    try:
        # 1. the demo corpus --------------------------------------------------
        warm_cache(demo)
        demo_bytes = [
            len(generator.generate(image_at(p, DEMO_CORPUS), MAX_EDGE)) for p in demo
        ]
        samples = time_each(
            demo,
            lambda p: generator.generate(image_at(p, DEMO_CORPUS), MAX_EDGE),
            REPEATS,
        )
        source_kb = statistics.fmean(p.stat().st_size for p in demo) / 1024
        print(
            f"1. generator alone, demo corpus: {len(demo)} photos, ~1024 px, "
            f"{source_kb:.0f} KB each on average"
        )
        print(describe("decode + resize + encode", samples))
        print(
            f"  thumbnail size: mean {statistics.fmean(demo_bytes) / 1024:.1f} KB, "
            f"median {statistics.median(demo_bytes) / 1024:.1f} KB, "
            f"max {max(demo_bytes) / 1024:.1f} KB"
        )
        demo_median = statistics.median(samples)
        print()

        # 2 and 3. large synthetic files ---------------------------------------
        started = time.perf_counter()
        corpora = build_large_corpus(workdir / "large", demo)
        print(
            f"2. generator alone, synthetic large JPEGs "
            f"(built in {time.perf_counter() - started:.1f} s under {workdir})"
        )
        large_medians: dict[str, float] = {}
        large_bytes: list[int] = []
        for label, paths in corpora.items():
            warm_cache(paths)
            root = paths[0].parent
            file_mb = statistics.fmean(p.stat().st_size for p in paths) / 1024 / 1024
            sizes = [
                len(generator.generate(image_at(p, root), MAX_EDGE)) for p in paths
            ]
            large_bytes.extend(sizes)
            adapter = time_each(
                paths, lambda p: generator.generate(image_at(p, root), MAX_EDGE), 3
            )
            naive = time_each(paths, naive_full_decode, 1)
            large_medians[label] = statistics.median(adapter)
            print(f"  {label}: {len(paths)} files, {file_mb:.1f} MB each")
            print(describe("adapter (draft, then orient)", adapter))
            print(describe("3. full decode first (naive)", naive))
            print(f"  thumbnail size: mean {statistics.fmean(sizes) / 1024:.1f} KB")
        print()

        # 4. the real pipeline --------------------------------------------------
        if os.environ.get("RFC030_SKIP_MODEL") == "1":
            print("4. pipeline with the real CLIP model: SKIPPED (RFC030_SKIP_MODEL=1)")
        else:
            from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel

            print(
                "4. IndexOrUpdateImagesUseCase with the real CLIP model "
                "(batch 8, SHA-256, in-memory repository)"
            )
            model = ClipEmbeddingModel()
            started = time.perf_counter()
            model.warm_up()
            print(
                f"  model loaded in {time.perf_counter() - started:.1f} s "
                "(not part of any figure below)"
            )
            workloads = {"demo corpus (45 x ~1024 px)": (demo, DEMO_CORPUS)}
            twenty = corpora["20 MP (5472x3648)"]
            workloads["20 MP synthetic"] = (twenty, twenty[0].parent)
            for label, (paths, root) in workloads.items():
                warm_cache(paths)
                run_pipeline(paths[:8], root, model, None)  # warm the forward pass
                cache = workdir / f"cache-{uuid.uuid4().hex[:6]}"
                writer = ThumbnailWriter(
                    generator, FilesystemThumbnailStore(cache), MAX_EDGE
                )
                without_1 = run_pipeline(paths, root, model, None)
                with_thumbs = run_pipeline(paths, root, model, writer)
                without_2 = run_pipeline(paths, root, model, None)
                print(f"  {label}:")
                for name, summary in (
                    ("without thumbnails (1st)", without_1),
                    ("with thumbnails", with_thumbs),
                    ("without thumbnails (2nd)", without_2),
                ):
                    rate = summary.discovered / summary.elapsed_seconds
                    print(
                        f"    {name:<26} {summary.elapsed_seconds:7.2f} s  "
                        f"{rate:5.2f} img/s  "
                        f"inference {summary.inference_seconds:6.2f} s  "
                        f"thumbnails {summary.thumbnail_seconds:5.2f} s "
                        f"({summary.thumbnails_written} written, "
                        f"{len(summary.thumbnail_failures)} failed)"
                    )
                baseline = (without_1.elapsed_seconds + without_2.elapsed_seconds) / 2
                added = with_thumbs.elapsed_seconds - baseline
                per_image = (
                    with_thumbs.thumbnail_seconds / with_thumbs.thumbnails_written
                )
                inference_per_image = (
                    with_thumbs.inference_seconds / with_thumbs.indexed
                )
                print(
                    f"    thumbnail time per image (from the summary): "
                    f"{per_image * 1000:.1f} ms, against inference "
                    f"{inference_per_image * 1000:.0f} ms per image in the same run "
                    f"({100 * per_image / inference_per_image:.1f}%)"
                )
                print(
                    f"    wall-clock added vs the mean of the two runs without: "
                    f"{added:+.2f} s ({100 * added / baseline:+.1f}%)"
                )
        print()

        # projection ------------------------------------------------------------
        print("projection")
        demo_kb = statistics.fmean(demo_bytes) / 1024
        large_kb = statistics.fmean(large_bytes) / 1024
        for label, kb in (("demo corpus", demo_kb), ("large synthetic", large_kb)):
            total_gb = kb * 100_000 / 1024 / 1024
            print(f"  100000 thumbnails x {kb:.1f} KB ({label}) = {total_gb:.2f} GB")
        for label, median in [("demo corpus", demo_median), *large_medians.items()]:
            hours = median * 100_000 / 1000 / 3600
            print(
                f"  100000 x {median:.1f} ms ({label}) = {hours:.2f} h of thumbnail "
                f"rendering; RFC-024 inference at {INFERENCE_MS_RFC_024:.0f} ms/image "
                f"= 12.5 h ({100 * median / INFERENCE_MS_RFC_024:.1f}%)"
            )
        print()
        print(
            "NOT MEASURED: cold reads from a spinning external disk. Every file "
            "above was in the OS cache; these are decode and encode costs, not "
            "seek costs. The large corpora are upscaled demo photos: real sensor "
            "noise compresses worse, so real source files are larger and may "
            "decode somewhat slower."
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
