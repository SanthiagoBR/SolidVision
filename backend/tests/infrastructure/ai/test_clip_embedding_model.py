"""Unit tests for the CLIP embedding adapter (RFC-023).

`CLIPModel` and `AutoProcessor` are replaced with stubs, so these tests run
offline, in milliseconds, and never touch Hugging Face -- while still driving
the adapter's real logic: opening the file with Pillow, converting to RGB,
pulling the embedding out of `.pooler_output`, validating the dimension,
L2-normalizing, and lifting the result into `EmbeddingVector`.

The stubs return genuine `BaseModelOutputWithPooling` objects holding genuine
torch tensors, because the shape of that return value is exactly the detail
this adapter exists to get right (see `_to_embedding_vector`). Behavior
against the real checkpoint is covered by `test_clip_embedding_model_slow.py`.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
import torch
from PIL import Image as PILImage
from PIL import UnidentifiedImageError
from transformers.modeling_outputs import BaseModelOutputWithPooling

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.clip_embedding_model import (
    TEXT_PROMPT_TEMPLATE,
    ClipEmbeddingModel,
)
from app.infrastructure.ai.query_translator import QueryTranslator

DIMENSION = 512
PRE_PROJECTION_WIDTH = 768

CLIP_MODEL_TARGET = (
    "app.infrastructure.ai.clip_embedding_model.CLIPModel.from_pretrained"
)
PROCESSOR_TARGET = (
    "app.infrastructure.ai.clip_embedding_model.AutoProcessor.from_pretrained"
)


def _float(tensor: torch.Tensor) -> torch.FloatTensor:
    """Satisfy the `FloatTensor` annotations on the transformers output type."""
    return cast(torch.FloatTensor, tensor)


class RecordingTranslator(QueryTranslator):
    """Records queries and optionally rewrites them, with no model involved."""

    def __init__(self, translation: str | None = None) -> None:
        self.seen: list[str] = []
        self._translation = translation

    def to_english(self, text: str) -> str:
        self.seen.append(text)
        return self._translation if self._translation is not None else text


class StubProcessor:
    """Stands in for `CLIPProcessor`, capturing what it was handed."""

    def __init__(self) -> None:
        self.images: list[PILImage.Image] = []
        self.texts: list[str] = []

    def __call__(
        self,
        images: PILImage.Image | None = None,
        text: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, torch.Tensor]:
        if images is not None:
            self.images.append(images)
            return {"pixel_values": torch.zeros(1, 3, 224, 224)}
        assert text is not None
        self.texts.extend(text)
        return {"input_ids": torch.ones(1, 7, dtype=torch.long)}


class StubClipModel:
    """Stands in for `CLIPModel`, mirroring the transformers 5.15 return shape."""

    def __init__(self, dimension: int = DIMENSION) -> None:
        self.dimension = dimension
        self.image_calls = 0
        self.text_calls = 0
        self.devices: list[torch.device] = []

    def eval(self) -> StubClipModel:
        return self

    def to(self, device: torch.device) -> StubClipModel:
        self.devices.append(device)
        return self

    def _features(self, seed: int) -> BaseModelOutputWithPooling:
        generator = torch.Generator().manual_seed(seed)
        pooled = (
            torch.rand(1, self.dimension, generator=generator) * 10.0
        )  # deliberately un-normalized, like the real checkpoint
        return BaseModelOutputWithPooling(
            # The wrong-but-tempting tensor: pre-projection encoder state,
            # 768-wide. An adapter reading this instead of `.pooler_output`
            # fails the dimension check rather than silently misbehaving.
            last_hidden_state=_float(torch.zeros(1, 50, PRE_PROJECTION_WIDTH)),
            pooler_output=_float(pooled),
        )

    def get_image_features(self, **kwargs: Any) -> BaseModelOutputWithPooling:
        self.image_calls += 1
        return self._features(seed=1)

    def get_text_features(self, **kwargs: Any) -> BaseModelOutputWithPooling:
        self.text_calls += 1
        return self._features(seed=2)


class StubLoads:
    """Counts checkpoint loads so reuse can be asserted."""

    def __init__(self, dimension: int = DIMENSION) -> None:
        self.model = StubClipModel(dimension)
        self.processor = StubProcessor()
        self.model_loads = 0
        self.processor_loads = 0
        self.requested_names: list[str] = []


@pytest.fixture()
def stub_clip(monkeypatch: pytest.MonkeyPatch) -> StubLoads:
    stubs = StubLoads()
    _install(monkeypatch, stubs)
    return stubs


def _install(monkeypatch: pytest.MonkeyPatch, stubs: StubLoads) -> None:
    def load_model(name: str, **kwargs: Any) -> StubClipModel:
        stubs.model_loads += 1
        stubs.requested_names.append(name)
        return stubs.model

    def load_processor(name: str, **kwargs: Any) -> StubProcessor:
        stubs.processor_loads += 1
        stubs.requested_names.append(name)
        return stubs.processor

    # Patched by dotted path rather than through an imported symbol: the
    # adapter module re-exports nothing, and mypy's strict no-implicit-reexport
    # rejects reaching into it for `CLIPModel`/`AutoProcessor`.
    monkeypatch.setattr(CLIP_MODEL_TARGET, load_model)
    monkeypatch.setattr(PROCESSOR_TARGET, load_processor)


def _adapter(translator: QueryTranslator | None = None) -> ClipEmbeddingModel:
    return ClipEmbeddingModel(
        device=torch.device("cpu"),
        translator=translator or RecordingTranslator(),
    )


@pytest.fixture()
def jpeg_path(tmp_path: Path) -> Path:
    path = tmp_path / "photo.jpg"
    PILImage.new("RGB", (120, 90), color=(30, 90, 150)).save(path, "JPEG")
    return path


def _image(path: Path) -> Image:
    image_path = ImagePath(str(path))
    return Image(
        id=ImageId(uuid.uuid4()),
        path=image_path,
        filename=path.stem,
        extension=path.suffix.lstrip(".").lower(),
    )


class TestContract:
    def test_adapter_implements_the_embedding_model_port(self) -> None:
        assert isinstance(_adapter(), EmbeddingModelPort)

    def test_constructing_the_adapter_loads_no_model(
        self, stub_clip: StubLoads
    ) -> None:
        """The lazy-singleton wiring in RFC-023 section 10 depends on this."""
        _adapter()

        assert stub_clip.model_loads == 0
        assert stub_clip.processor_loads == 0

    def test_the_configured_checkpoint_is_the_one_the_bake_off_selected(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        _adapter().encode_image(_image(jpeg_path))

        assert set(stub_clip.requested_names) == {
            "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
        }


class TestImageEncoding:
    def test_returns_an_embedding_vector(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        result = _adapter().encode_image(_image(jpeg_path))

        assert isinstance(result, EmbeddingVector)

    def test_embedding_has_the_configured_dimension(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        result = _adapter().encode_image(_image(jpeg_path))

        assert len(result.values) == DIMENSION

    def test_embedding_is_l2_normalized(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        """Cosine similarity is what the HNSW index is built for.

        The checkpoint's raw `pooler_output` is not normalized -- measured
        norms are ~11.6 for images -- so the adapter must do it (RFC-023 6).
        """
        result = _adapter().encode_image(_image(jpeg_path))

        norm = math.sqrt(sum(value * value for value in result.values))
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_the_file_on_disk_is_actually_opened_and_decoded(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        """Unlike `FakeEmbeddingModel`, real pixels must reach the processor."""
        _adapter().encode_image(_image(jpeg_path))

        (received,) = stub_clip.processor.images
        assert received.size == (120, 90)

    def test_images_are_converted_to_rgb(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """CLIP wants 3-channel RGB; RFC-022's corpus is not all RGB."""
        path = tmp_path / "grayscale.jpg"
        PILImage.new("L", (64, 48), color=120).save(path, "JPEG")

        _adapter().encode_image(_image(path))

        (received,) = stub_clip.processor.images
        assert received.mode == "RGB"

    @pytest.mark.parametrize(
        ("mode", "suffix"),
        [("L", "jpg"), ("RGBA", "png"), ("CMYK", "jpg"), ("I;16", "tiff")],
        ids=["grayscale", "alpha", "cmyk", "sixteen_bit"],
    )
    def test_non_rgb_hard_cases_decode_without_special_casing(
        self, mode: str, suffix: str, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """The RFC-022 6.2 colorspace cases must all survive the conversion.

        Each mode is paired with a container that can actually hold it --
        PNG has no CMYK, JPEG has no alpha -- mirroring how the hard-case
        generator writes them.
        """
        path = tmp_path / f"case.{suffix}"
        PILImage.new(mode, (40, 30)).save(path)

        result = _adapter().encode_image(_image(path))

        assert len(result.values) == DIMENSION

    def test_the_same_file_encodes_deterministically(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        adapter = _adapter()
        image = _image(jpeg_path)

        assert adapter.encode_image(image).values == adapter.encode_image(image).values


class TestImageErrorsPropagate:
    """RFC-023 section 4: `IndexingWorker` owns per-file isolation, not this."""

    def test_zero_byte_file_raises(self, stub_clip: StubLoads, tmp_path: Path) -> None:
        path = tmp_path / "zero_byte.jpg"
        path.write_bytes(b"")

        with pytest.raises(UnidentifiedImageError):
            _adapter().encode_image(_image(path))

    def test_truncated_file_raises(self, stub_clip: StubLoads, tmp_path: Path) -> None:
        buffer = tmp_path / "full.jpg"
        PILImage.new("RGB", (200, 150), color=(10, 20, 30)).save(
            buffer, "JPEG", quality=95
        )
        payload = buffer.read_bytes()
        path = tmp_path / "truncated.jpg"
        path.write_bytes(payload[: len(payload) * 6 // 10])

        with pytest.raises(OSError):
            _adapter().encode_image(_image(path))

    def test_missing_file_raises(self, stub_clip: StubLoads, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _adapter().encode_image(_image(tmp_path / "absent.jpg"))

    def test_a_decoding_failure_leaves_no_partial_state(
        self, stub_clip: StubLoads, tmp_path: Path, jpeg_path: Path
    ) -> None:
        """A corrupt file must not poison the adapter for the next file."""
        broken = tmp_path / "zero_byte.jpg"
        broken.write_bytes(b"")
        adapter = _adapter()

        with pytest.raises(UnidentifiedImageError):
            adapter.encode_image(_image(broken))

        assert len(adapter.encode_image(_image(jpeg_path)).values) == DIMENSION


class TestTextEncoding:
    def test_returns_an_embedding_vector_of_the_configured_dimension(
        self, stub_clip: StubLoads
    ) -> None:
        result = _adapter().encode_text("rural property with a small lake")

        assert isinstance(result, EmbeddingVector)
        assert len(result.values) == DIMENSION

    def test_embedding_is_l2_normalized(self, stub_clip: StubLoads) -> None:
        result = _adapter().encode_text("a bicycle parked on a street")

        norm = math.sqrt(sum(value * value for value in result.values))
        assert norm == pytest.approx(1.0, abs=1e-6)

    def test_english_query_is_wrapped_in_the_bake_off_template(
        self, stub_clip: StubLoads
    ) -> None:
        _adapter().encode_text("rural property with a small lake")

        assert stub_clip.processor.texts == [
            "a photo of rural property with a small lake"
        ]

    def test_the_template_is_the_one_the_bake_off_chose(self) -> None:
        """`an aerial photo of {query}` scored worse and must not creep back."""
        assert TEXT_PROMPT_TEMPLATE == "a photo of {query}"
        assert "aerial" not in TEXT_PROMPT_TEMPLATE

    def test_only_one_prompt_is_encoded_per_query(self, stub_clip: StubLoads) -> None:
        """No prompt ensembling in RFC-023 (section 7)."""
        _adapter().encode_text("commercial street with shops")

        assert len(stub_clip.processor.texts) == 1

    def test_build_prompt_applies_the_template(self, stub_clip: StubLoads) -> None:
        assert _adapter().build_prompt("a lake") == "a photo of a lake"


class TestTranslationRouting:
    def test_translated_text_is_what_reaches_the_template(
        self, stub_clip: StubLoads
    ) -> None:
        translator = RecordingTranslator(translation="rural property with a small lake")

        prompt = _adapter(translator).build_prompt(
            "propriedade rural com um pequeno lago"
        )

        assert prompt == "a photo of rural property with a small lake"

    def test_every_query_is_offered_to_the_translator(
        self, stub_clip: StubLoads
    ) -> None:
        translator = RecordingTranslator()

        _adapter(translator).encode_text("commercial street with shops")

        assert translator.seen == ["commercial street with shops"]

    def test_english_text_passes_through_unchanged(self, stub_clip: StubLoads) -> None:
        """A pass-through translator must not alter the resulting prompt."""
        query = "warehouse next to a highway"

        assert _adapter().build_prompt(query) == f"a photo of {query}"

    def test_images_are_never_routed_through_translation(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        translator = RecordingTranslator()

        _adapter(translator).encode_image(_image(jpeg_path))

        assert translator.seen == []


class TestModelLifecycle:
    def test_the_model_is_loaded_once_across_many_calls(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        adapter = _adapter()

        adapter.encode_image(_image(jpeg_path))
        adapter.encode_image(_image(jpeg_path))
        adapter.encode_text("a lake")
        adapter.encode_text("a farm")

        assert stub_clip.model_loads == 1
        assert stub_clip.processor_loads == 1

    def test_image_and_text_share_one_model_instance(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        """One checkpoint, one shared space -- not two independently loaded ones."""
        adapter = _adapter()

        adapter.encode_image(_image(jpeg_path))
        adapter.encode_text("a lake")

        assert stub_clip.model.image_calls == 1
        assert stub_clip.model.text_calls == 1
        assert stub_clip.model_loads == 1

    def test_the_model_is_moved_to_the_resolved_device(
        self, stub_clip: StubLoads, jpeg_path: Path
    ) -> None:
        _adapter().encode_image(_image(jpeg_path))

        assert stub_clip.model.devices == [torch.device("cpu")]


class TestFeatureExtraction:
    def test_a_dimension_mismatch_is_rejected_loudly(
        self, monkeypatch: pytest.MonkeyPatch, jpeg_path: Path
    ) -> None:
        """A checkpoint whose width disagrees with the schema must not persist.

        This is also the guard against reading `.last_hidden_state`, the
        768-wide pre-projection state, instead of `.pooler_output`.
        """
        stubs = StubLoads(dimension=PRE_PROJECTION_WIDTH)
        _install(monkeypatch, stubs)

        with pytest.raises(ValueError, match="768 dimensions"):
            _adapter().encode_image(_image(jpeg_path))

    def test_the_error_names_the_configured_dimension(
        self, monkeypatch: pytest.MonkeyPatch, jpeg_path: Path
    ) -> None:
        stubs = StubLoads(dimension=256)
        _install(monkeypatch, stubs)

        with pytest.raises(ValueError, match="settings.embedding_dimension is 512"):
            _adapter().encode_image(_image(jpeg_path))

    def test_an_unexpected_feature_shape_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, jpeg_path: Path
    ) -> None:
        class BadShapeModel(StubClipModel):
            def get_image_features(self, **kwargs: Any) -> BaseModelOutputWithPooling:
                return BaseModelOutputWithPooling(
                    last_hidden_state=_float(torch.zeros(1, 50, PRE_PROJECTION_WIDTH)),
                    pooler_output=_float(torch.rand(3, DIMENSION)),
                )

        stubs = StubLoads()
        stubs.model = BadShapeModel()
        _install(monkeypatch, stubs)

        with pytest.raises(ValueError, match="expected a single"):
            _adapter().encode_image(_image(jpeg_path))
