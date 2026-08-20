"""Production CLIP embedding adapter (RFC-023).

Implements `EmbeddingModelPort` with `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`,
the checkpoint the RFC-023 bake-off selected. Every CLIP, torch, and
transformers detail is confined to this package: Domain and Application see
only `EmbeddingModelPort`, `Image`, and `EmbeddingVector`, so a future RFC can
swap in a fine-tuned CLIP, SigLIP, or an ONNX backend without touching a
single contract above Infrastructure.
"""

from __future__ import annotations

from collections.abc import Mapping

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
        """Decode the pixels at `image.path` and return their CLIP embedding."""
        model, processor = self._ensure_loaded()

        with PILImage.open(image.path.value) as opened:
            # CLIP was trained on 3-channel RGB. Converting explicitly here
            # rather than leaning on processor internals puts the RFC-022 6.2
            # hard cases -- grayscale, CMYK, 16-bit TIFF, RGBA -- on one
            # documented path. `convert()` also forces the decode that
            # Pillow's lazy `open()` defers, so a zero-byte or truncated file
            # fails here rather than somewhere deeper in the model.
            pixels = opened.convert("RGB")
            inputs = processor(images=pixels, return_tensors="pt")

        with torch.no_grad():
            features = model.get_image_features(**self._to_device(inputs))

        return self._to_embedding_vector(features.pooler_output)

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

        return self._to_embedding_vector(features.pooler_output)

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

    def _to_embedding_vector(self, features: torch.Tensor) -> EmbeddingVector:
        """Validate, L2-normalize, and lift CLIP features into the domain type.

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
        if features.ndim != 2 or features.shape[0] != 1:
            raise ValueError(
                f"{self._model_name} returned features of shape "
                f"{tuple(features.shape)}; expected a single (1, N) embedding"
            )

        dimension = features.shape[1]
        if dimension != self._expected_dimension:
            raise ValueError(
                f"{self._model_name} produced {dimension} dimensions, but "
                f"settings.embedding_dimension is {self._expected_dimension}"
            )

        normalized = torch.nn.functional.normalize(features, p=2.0, dim=-1)
        return EmbeddingVector(normalized[0].detach().cpu().tolist())
