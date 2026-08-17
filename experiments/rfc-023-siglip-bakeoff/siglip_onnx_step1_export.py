"""RFC-023 ONNX check, step 1/3: export both SigLIP towers to ONNX (fp32).

Split into its own process, not a function called from the orchestrator,
because the first attempt at this whole check crashed the machine --
see ../README.md for the postmortem. The suspected cause was holding the
eager PyTorch model and up to four ONNX Runtime sessions in memory at
once; running each heavy phase as its own OS process guarantees the OS
reclaims that memory on exit regardless of anything either library holds
onto internally, which del/gc.collect() inside one long process does not
reliably do for native (C++) allocators.

This step needs the eager PyTorch model (export requires tracing it);
steps 2 and 3 only touch ONNX Runtime and never import torch's model
loading path, so they cannot hold PyTorch model memory even by accident.

Run:
    python siglip_onnx_step1_export.py
"""

from __future__ import annotations

import sys
from pathlib import Path

MODEL_ID = "google/siglip2-base-patch16-384"


def find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "backend" / "dataset_tools" / "manifest.py").is_file():
            return candidate
    raise SystemExit(f"Could not locate the SolidVision repo root by walking up from {start}.")


REPO_ROOT = find_repo_root(Path(__file__).resolve())
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

import torch  # noqa: E402
from PIL import Image  # noqa: E402
from transformers import AutoModel, AutoProcessor  # noqa: E402

from dataset_tools.manifest import DEMO_CORPUS_ROOT, DEMO_MANIFEST_PATH, load_manifest  # noqa: E402

EXPORT_DIR = Path(__file__).with_name("onnx_export")


class VisionWrapper(torch.nn.Module):
    def __init__(self, vision_model: torch.nn.Module) -> None:
        super().__init__()
        self.vision_model = vision_model

    def forward(self, pixel_values: torch.Tensor) -> torch.Tensor:
        features = self.vision_model(pixel_values=pixel_values).pooler_output
        return features / features.norm(p=2, dim=-1, keepdim=True)


class TextWrapper(torch.nn.Module):
    def __init__(self, text_model: torch.nn.Module) -> None:
        super().__init__()
        self.text_model = text_model

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        features = self.text_model(input_ids=input_ids).pooler_output
        return features / features.norm(p=2, dim=-1, keepdim=True)


def main() -> None:
    EXPORT_DIR.mkdir(exist_ok=True)

    manifest = load_manifest(DEMO_MANIFEST_PATH)
    manifest.verify_matches_directory(DEMO_CORPUS_ROOT)
    sample_path = DEMO_CORPUS_ROOT / manifest.images[0].relative_path
    sample_image = Image.open(sample_path).convert("RGB")

    print(f"Loading {MODEL_ID} (fp32, eager) for export only...")
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID, dtype=torch.float32)
    model.eval()

    vision_path = EXPORT_DIR / "vision_fp32.onnx"
    vision_wrapper = VisionWrapper(model.vision_model).eval()
    dummy_pixels = processor(images=[sample_image, sample_image], return_tensors="pt")["pixel_values"]
    torch.onnx.export(
        vision_wrapper, (dummy_pixels,), str(vision_path),
        input_names=["pixel_values"], output_names=["image_embeds"],
        dynamic_axes={"pixel_values": {0: "batch"}, "image_embeds": {0: "batch"}},
        opset_version=17, dynamo=False,
    )
    print(f"  wrote {vision_path}")

    text_path = EXPORT_DIR / "text_fp32.onnx"
    text_wrapper = TextWrapper(model.text_model).eval()
    dummy_ids = processor(text=["a photo", "another photo"], padding="max_length", return_tensors="pt")["input_ids"]
    torch.onnx.export(
        text_wrapper, (dummy_ids,), str(text_path),
        input_names=["input_ids"], output_names=["text_embeds"],
        dynamic_axes={"input_ids": {0: "batch"}, "text_embeds": {0: "batch"}},
        opset_version=17, dynamo=False,
    )
    print(f"  wrote {text_path}")
    print("\nExport complete. This process is about to exit, releasing all PyTorch memory.")


if __name__ == "__main__":
    main()
