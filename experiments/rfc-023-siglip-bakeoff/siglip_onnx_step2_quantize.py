"""RFC-023 ONNX check, step 2/3: calibrate and statically quantize the vision tower.

Own process, same reasoning as step 1 -- see that file's docstring and
../README.md's postmortem on the crash from running all of this in one
process. This step never imports torch's model-loading path at all, only
onnxruntime and the HF processor (for turning images into the same
tensors the model would see) -- there is no eager PyTorch model in memory
here, only the already-exported ONNX graph from step 1.

Vision tower only -- text-tower quantization was tried and abandoned
(see git history for that attempt). Then this vision-only version was
ALSO killed once already with 45 calibration images (16.19GB then
11.84GB private memory, two separate runs, both approaching this
machine's 16GB ceiling before being force-killed).

Root cause, confirmed via microsoft/onnxruntime#21979 (open, unresolved,
reported against 1.19.0, still present in 1.28.0 here): the Calibrator's
collect_data method retains every intermediate activation from every
calibration image, for every node in the graph, simultaneously -- it
computes nothing incrementally and discards nothing. Memory grows with
the number of calibration images processed, not with model size. The
issue reporter's own workaround: "can only calibrate with a couple
images." No fix version is documented after roughly two years open, so
this is not something a different onnxruntime version is expected to
resolve.

CALIBRATION_IMAGE_COUNT is deliberately tiny (8, one batch) as a direct
application of that workaround -- this is the last attempt agreed before
abandoning ONNX Runtime static quantization entirely; if this still
grows dangerously, the technique is not usable in this environment
regardless of further tuning.

Run:
    python siglip_onnx_step2_quantize.py
"""

from __future__ import annotations

import sys
from pathlib import Path

CALIBRATION_IMAGE_COUNT = 8  # single batch -- see root-cause note above
CALIBRATION_BATCH_SIZE = 8


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "dataset_tools" / "manifest.py").is_file():
            return candidate
    raise SystemExit(f"Could not locate the SolidVision repo root by walking up from {start}.")


REPO_ROOT = find_repo_root(Path(__file__).resolve())
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from onnxruntime.quantization import (  # noqa: E402
    CalibrationDataReader,
    CalibrationMethod,
    QuantFormat,
    QuantType,
    quantize_static,
)
from PIL import Image  # noqa: E402
from transformers import AutoProcessor  # noqa: E402

from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest  # noqa: E402

MODEL_ID = "google/siglip2-base-patch16-384"
EXPORT_DIR = Path(__file__).with_name("onnx_export")


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class VisionCalibrationReader(CalibrationDataReader):
    def __init__(self, processor, image_paths: list[Path], batch_size: int) -> None:
        batches = chunk(image_paths, batch_size)
        self._iter = iter(self._make_batch(processor, b) for b in batches)

    @staticmethod
    def _make_batch(processor, paths: list[Path]) -> dict:
        images = [Image.open(p).convert("RGB") for p in paths]
        pixel_values = processor(images=images, return_tensors="pt")["pixel_values"].numpy()
        return {"pixel_values": pixel_values}

    def get_next(self):
        return next(self._iter, None)


def main() -> None:
    vision_fp32_path = EXPORT_DIR / "vision_fp32.onnx"
    if not vision_fp32_path.exists():
        raise SystemExit("Run siglip_onnx_step1_export.py first -- vision_fp32.onnx not found.")

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    all_image_paths = [DEMO_CORPUS_ROOT / img.relative_path for img in manifest.images]
    image_paths = all_image_paths[:CALIBRATION_IMAGE_COUNT]

    processor = AutoProcessor.from_pretrained(MODEL_ID)

    vision_int8_path = vision_fp32_path.with_name("vision_int8.onnx")
    print(f"Quantizing vision tower ({len(image_paths)} calibration images out of "
          f"{len(all_image_paths)} available, batch size {CALIBRATION_BATCH_SIZE}, "
          f"per-tensor)...", flush=True)
    print("Starting quantize_static() -- this is the call that grew unboundedly "
          "before (microsoft/onnxruntime#21979); watching memory externally now.",
          flush=True)
    quantize_static(
        model_input=str(vision_fp32_path),
        model_output=str(vision_int8_path),
        calibration_data_reader=VisionCalibrationReader(processor, image_paths, CALIBRATION_BATCH_SIZE),
        quant_format=QuantFormat.QDQ,
        calibrate_method=CalibrationMethod.MinMax,
        weight_type=QuantType.QInt8,
        activation_type=QuantType.QUInt8,
        per_channel=False,
    )
    print(f"  wrote {vision_int8_path}")
    print("\nQuantization complete. This process is about to exit, releasing all memory.")


if __name__ == "__main__":
    main()
