"""Production CLIP embedding adapter (RFC-023).

Implements `EmbeddingModelPort` with `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`,
the checkpoint the RFC-023 bake-off selected. Every CLIP, torch, and
transformers detail is confined to this package: Domain and Application see
only `EmbeddingModelPort`, `Image`, and `EmbeddingVector`, so a future RFC can
swap in a fine-tuned CLIP, SigLIP, or an ONNX backend without touching a
single contract above Infrastructure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import torch
from PIL import Image as PILImage
from transformers import AutoProcessor, CLIPModel
from transformers.processing_utils import ProcessorMixin

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.ai.device import resolve_device
from app.infrastructure.ai.query_translator import (
    MarianQueryTranslator,
    QueryTranslator,
)
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

# Fixed by the bake-off, not a tunable. `a photo of {query}` scored 60.0%
# strict-EN / 80.4% pairwise-EN on the selected checkpoint against 56.0% /
# 78.8% for the bare query and 52.0% / 73.5% for `an aerial photo of {query}`
# (`clip_template_run_output.log`). Despite this being an aerial-imagery
# product, the aerial-specific template was the worst of the three.
TEXT_PROMPT_TEMPLATE = "a photo of {query}"


class ClipEmbeddingModel(EmbeddingModelPort):
    """Encode images and text into one 512-dimensional CLIP similarity space.

    Both models are loaded lazily on first use and then reused for the life
    of the instance, so constructing the adapter -- which the dependency
    wiring does at import-adjacent time -- costs nothing and never touches
    the network (RFC-023 section 10).

    Errors from Pillow and from model inference deliberately propagate.
    `IndexingWorker.run()` already isolates failures per file, so a second
    generic `except` here would only hide which file failed and why.
    """

    def __init__(
        self,
        model_name: str | None = None,
        expected_dimension: int | None = None,
        device: torch.device | None = None,
        translator: QueryTranslator | None = None,
    ) -> None:
        self._model_name = model_name or settings.embedding_model
        self._expected_dimension = expected_dimension or settings.embedding_dimension
        self._device = device if device is not None else resolve_device()
        self._translator = translator or MarianQueryTranslator(device=self._device)
        self._model: CLIPModel | None = None
        self._processor: ProcessorMixin | None = None

    def encode_image(self, image: Image) -> EmbeddingVector:
        """Decode the image's pixels and return their CLIP embedding."""
        model, processor = self._ensure_loaded()
        pixel_values = self._preprocess(processor, image)

        with torch.no_grad():
            features = model.get_image_features(
                pixel_values=pixel_values.to(self._device)
            )

        return self._to_embedding_vectors(features.pooler_output, expected_rows=1)[0]

    def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
        """Encode several images in one forward pass (RFC-024 section 5).

        Overrides the port's per-image default because CLIP genuinely does
        have a faster way to do this: measured on the demo corpus on CPU, a
        batch of 8 runs at 1.40x the throughput of a batch of 1, with the
        embeddings unchanged to within floating point.

        Preprocessing is deliberately a loop, not a single
        `processor(images=[...])` call. Each iteration decodes one file to
        full size, shrinks it to the model's fixed input, and drops the
        full-size copy before the next file is opened, so what accumulates
        across the batch is N small tensors rather than N decoded photos.
        That distinction is invisible on the 1024x768 demo corpus (~1.8 MB
        each) and decides whether the pipeline survives a directory of
        24-megapixel photos, where the decoded originals would be ~72 MB
        apiece (RFC-024 section 9).

        Errors propagate, exactly as in `encode_image()`. One unreadable
        file therefore fails the whole batch, which is intentional: the
        caller retries the batch one image at a time to find out which file
        it was, and RFC-023 section 4's decision that this adapter swallows
        nothing still stands.
        """
        if not images:
            return []

        model, processor = self._ensure_loaded()
        pixel_values = torch.cat(
            [self._preprocess(processor, image) for image in images], dim=0
        )

        with torch.no_grad():
            features = model.get_image_features(
                pixel_values=pixel_values.to(self._device)
            )

        return self._to_embedding_vectors(features.pooler_output, len(images))

    def _preprocess(self, processor: ProcessorMixin, image: Image) -> torch.Tensor:
        """Reduce one file on disk to the fixed-size tensor the model consumes.

        CLIP was trained on 3-channel RGB. Converting explicitly here rather
        than leaning on processor internals puts the RFC-022 6.2 hard cases
        -- grayscale, CMYK, 16-bit TIFF, RGBA -- on one documented path.
        `convert()` also forces the decode that Pillow's lazy `open()`
        defers, so a zero-byte or truncated file fails here rather than
        somewhere deeper in the model.

        The full-size decoded image is a local of this method and nothing
        else, so it becomes unreachable the moment the method returns. That
        is the whole memory strategy: only the returned tensor -- fixed at
        the model's input resolution, regardless of the source photo -- is
        allowed to outlive one file.
        """
        with PILImage.open(image.require_absolute_path().value) as opened:
            pixels = opened.convert("RGB")
            inputs: Mapping[str, torch.Tensor] = processor(
                images=pixels, return_tensors="pt"
            )

        return inputs["pixel_values"]

    def encode_text(self, text: str) -> EmbeddingVector:
        """Encode a user query into the same space `encode_image` writes into."""
        model, processor = self._ensure_loaded()

        inputs = processor(
            text=[self.build_prompt(text)],
            return_tensors="pt",
            padding=True,
        )

        with torch.no_grad():
            features = model.get_text_features(**self._to_device(inputs))

        return self._to_embedding_vectors(features.pooler_output, expected_rows=1)[0]

    def build_prompt(self, text: str) -> str:
        """Return the exact English string handed to CLIP's text encoder.

        The full query pipeline in one line: detect the language, translate
        Portuguese to English, then apply the template. Public so the prompt
        and translation behavior can be asserted without loading CLIP itself.
        """
        return TEXT_PROMPT_TEMPLATE.format(query=self._translator.to_english(text))

    def _ensure_loaded(self) -> tuple[CLIPModel, ProcessorMixin]:
        """Load the checkpoint on first use and reuse it on every later call."""
        if self._model is None or self._processor is None:
            logger.info("Loading CLIP model %s onto %s", self._model_name, self._device)
            model = CLIPModel.from_pretrained(self._model_name)
            # Three upstream typing gaps in transformers 5.15, not real type
            # errors: `nn.Module.eval` is unannotated, `PreTrainedModel.to`
            # is wrapped in a decorator whose signature mypy reads as taking
            # the model itself, and `AutoProcessor.from_pretrained` is
            # unannotated. `warn_unused_ignores` will flag each of these the
            # moment a release types them properly.
            model.eval()  # type: ignore[no-untyped-call]
            self._model = model.to(self._device)  # type: ignore[arg-type]
            self._processor = AutoProcessor.from_pretrained(  # type: ignore[no-untyped-call]
                self._model_name
            )

        return self._model, self._processor

    def _to_device(self, inputs: Mapping[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        return {key: value.to(self._device) for key, value in inputs.items()}

    def _to_embedding_vectors(
        self, features: torch.Tensor, expected_rows: int
    ) -> list[EmbeddingVector]:
        """Validate, L2-normalize, and lift CLIP features into the domain type.

        Takes `expected_rows` rather than assuming one, so that a batch of
        N and a batch of 1 are validated and normalized by the same code.
        `normalize(..., dim=-1)` is per-row, so batching cannot change any
        individual embedding -- which is what makes RFC-024's equivalence
        requirement a property of the arithmetic rather than a hope.

        Two transformers-version-specific facts drive this method, both
        verified against the installed transformers 5.15 rather than copied
        from an example:

        1. `get_image_features()` / `get_text_features()` do NOT return a
           tensor. They return a `BaseModelOutputWithPooling` whose
           `.pooler_output` the method has overwritten with the projected
           embedding. `.last_hidden_state` on that same object is the
           pre-projection 768-d encoder state -- the wrong space entirely,
           and silently the wrong size.
        2. Neither output is normalized: measured L2 norms are ~11.6 for
           images and ~8.5 for text. Cosine similarity is the metric the
           HNSW index is built for (`vector_cosine_ops`), so normalizing is
           required, and it belongs here rather than in `EmbeddingVector` --
           that value object is model-agnostic and must not impose one
           embedding space's convention on every future model (RFC-023 6).
        """
        if features.ndim != 2 or features.shape[0] != expected_rows:
            wanted = (
                "a single (1, N) embedding"
                if expected_rows == 1
                else f"{expected_rows} ({expected_rows}, N) embeddings"
            )
            raise ValueError(
                f"{self._model_name} returned features of shape "
                f"{tuple(features.shape)}; expected {wanted}"
            )

        dimension = features.shape[1]
        if dimension != self._expected_dimension:
            raise ValueError(
                f"{self._model_name} produced {dimension} dimensions, but "
                f"settings.embedding_dimension is {self._expected_dimension}"
            )

        normalized = torch.nn.functional.normalize(features, p=2.0, dim=-1)
        return [EmbeddingVector(row) for row in normalized.detach().cpu().tolist()]
