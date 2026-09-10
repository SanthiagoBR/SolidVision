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

import gc
import math
import uuid
import weakref
import zlib
from pathlib import Path
from typing import Any, cast

import pytest
import torch
from PIL import Image as PILImage
from PIL import UnidentifiedImageError
from tests.conftest import TEST_DEVICE_ID
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
    """Stands in for `CLIPProcessor`, capturing what it was handed.

    The returned `pixel_values` depend on the image's actual bytes rather
    than being a constant zero tensor. Without that, every row of a batch
    would be identical and "batched output matches single output" would be
    true no matter how badly the adapter sliced or reordered things.
    """

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
            return {"pixel_values": _fingerprint_tensor(images)}
        assert text is not None
        self.texts.extend(text)
        return {"input_ids": torch.ones(1, 7, dtype=torch.long)}


def _fingerprint_tensor(image: PILImage.Image) -> torch.Tensor:
    """One fixed-size tensor whose value is derived from the image content."""
    value = (zlib.crc32(image.tobytes()) % 100_003) / 100_003.0
    return torch.full((1, 3, 8, 8), value, dtype=torch.float32)


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

    def _content_rows(self, pixel_values: torch.Tensor) -> torch.FloatTensor:
        """Derive one un-normalized feature row per input row, from its content.

        Position-independent on purpose: row `i` of a batch must come out
        the same as the sole row of a batch of one holding the same image,
        which is exactly what RFC-024's equivalence requirement means.
        """
        ramp = torch.arange(self.dimension, dtype=torch.float32) * 0.001
        rows = [
            (ramp + float(pixel_values[index].mean()) + 1.0) * 7.0
            for index in range(pixel_values.shape[0])
        ]
        return _float(torch.stack(rows))

    def get_image_features(self, **kwargs: Any) -> BaseModelOutputWithPooling:
        self.image_calls += 1
        pixel_values = kwargs["pixel_values"]
        pooled = self._content_rows(pixel_values)
        return BaseModelOutputWithPooling(
            last_hidden_state=_float(
                torch.zeros(pooled.shape[0], 50, PRE_PROJECTION_WIDTH)
            ),
            pooler_output=pooled,
        )

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
        device_id=TEST_DEVICE_ID,
        relative_path=image_path,
        absolute_path=image_path,
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


class TestBatchImageEncoding:
    """RFC-024 section 5: the adapter's own batch path."""

    @staticmethod
    def _photos(tmp_path: Path, count: int) -> list[Image]:
        images = []
        for index in range(count):
            path = tmp_path / f"photo_{index}.jpg"
            PILImage.new(
                "RGB", (64, 48), color=(20 + index * 20, 40, 200 - index * 15)
            ).save(path, "JPEG", quality=95)
            images.append(_image(path))
        return images

    def test_returns_one_embedding_per_image(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        results = _adapter().encode_images(self._photos(tmp_path, 5))

        assert len(results) == 5
        assert all(isinstance(result, EmbeddingVector) for result in results)
        assert all(len(result.values) == DIMENSION for result in results)

    def test_an_empty_batch_encodes_nothing_and_loads_nothing(
        self, stub_clip: StubLoads
    ) -> None:
        assert _adapter().encode_images([]) == []
        assert stub_clip.model_loads == 0

    def test_the_whole_batch_is_one_forward_pass(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """The entire point: N images, one call into the model."""
        _adapter().encode_images(self._photos(tmp_path, 6))

        assert stub_clip.model.image_calls == 1

    def test_every_image_is_preprocessed_individually(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """Preprocessing is per file so full-size decodes never accumulate."""
        _adapter().encode_images(self._photos(tmp_path, 4))

        assert len(stub_clip.processor.images) == 4

    def test_batched_embeddings_match_single_image_embeddings(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """Batching changes how the work is done, never what is computed."""
        images = self._photos(tmp_path, 5)
        adapter = _adapter()

        batched = adapter.encode_images(images)
        one_at_a_time = [adapter.encode_image(image) for image in images]

        for from_batch, alone in zip(batched, one_at_a_time, strict=True):
            assert from_batch.values == pytest.approx(alone.values, abs=1e-6)

    def test_results_come_back_in_the_order_they_were_given(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """A reordered batch would silently attach embeddings to wrong rows."""
        images = self._photos(tmp_path, 4)
        adapter = _adapter()

        forwards = adapter.encode_images(images)
        backwards = adapter.encode_images(list(reversed(images)))

        for index, expected in enumerate(forwards):
            assert backwards[len(images) - 1 - index].values == pytest.approx(
                expected.values, abs=1e-6
            )

    def test_every_batched_embedding_is_l2_normalized(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        results = _adapter().encode_images(self._photos(tmp_path, 4))

        for result in results:
            norm = math.sqrt(sum(value * value for value in result.values))
            assert norm == pytest.approx(1.0, abs=1e-6)

    def test_a_broken_file_fails_the_batch_rather_than_being_swallowed(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """RFC-023 section 4 stands: the caller retries to find the culprit."""
        images = self._photos(tmp_path, 3)
        broken = tmp_path / "zero_byte.jpg"
        broken.write_bytes(b"")

        with pytest.raises(UnidentifiedImageError):
            _adapter().encode_images([*images, _image(broken)])

    def test_a_failed_batch_leaves_the_adapter_usable(
        self, stub_clip: StubLoads, tmp_path: Path
    ) -> None:
        """The per-image retry that follows a failed batch depends on this."""
        images = self._photos(tmp_path, 3)
        broken = tmp_path / "zero_byte.jpg"
        broken.write_bytes(b"")
        adapter = _adapter()

        with pytest.raises(UnidentifiedImageError):
            adapter.encode_images([*images, _image(broken)])

        assert len(adapter.encode_images(images)) == 3

    def test_a_feature_row_count_mismatch_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A model returning fewer rows than images must not be silently zipped."""

        class OneRowModel(StubClipModel):
            def get_image_features(self, **kwargs: Any) -> BaseModelOutputWithPooling:
                return BaseModelOutputWithPooling(
                    last_hidden_state=_float(torch.zeros(1, 50, PRE_PROJECTION_WIDTH)),
                    pooler_output=_float(torch.rand(1, DIMENSION)),
                )

        stubs = StubLoads()
        stubs.model = OneRowModel()
        _install(monkeypatch, stubs)

        with pytest.raises(ValueError, match=r"expected 3 \(3, N\) embeddings"):
            _adapter().encode_images(self._photos(tmp_path, 3))


class TestBatchMemoryCeiling:
    """RFC-024 section 9: N in a batch must not mean N decoded photos in RAM."""

    class _LivenessTrackingProcessor(StubProcessor):
        """Watches how many full-size decoded images are alive at once.

        Deliberately does not retain the images it is handed -- the parent
        class keeps them in a list for other assertions, and holding a
        reference is precisely what this test is trying to detect.
        """

        def __init__(self) -> None:
            super().__init__()
            self.live = 0
            self.peak_live = 0

        def __call__(
            self,
            images: PILImage.Image | None = None,
            text: list[str] | None = None,
            **kwargs: Any,
        ) -> dict[str, torch.Tensor]:
            if images is None:
                return super().__call__(images=images, text=text, **kwargs)

            self.live += 1
            self.peak_live = max(self.peak_live, self.live)
            weakref.finalize(images, self._released)
            return {"pixel_values": _fingerprint_tensor(images)}

        def _released(self) -> None:
            self.live -= 1

    @pytest.fixture()
    def tracking(self, monkeypatch: pytest.MonkeyPatch) -> _LivenessTrackingProcessor:
        stubs = StubLoads()
        processor = TestBatchMemoryCeiling._LivenessTrackingProcessor()
        stubs.processor = processor
        _install(monkeypatch, stubs)
        return processor

    @staticmethod
    def _large_photos(tmp_path: Path, count: int) -> list[Image]:
        """Images big enough that retaining them all would be the bug.

        Small next to a 24-megapixel drone photo, but the property under
        test is structural -- how many are held at once -- not how big any
        one of them is, so there is no reason to make the suite slow to
        assert it.
        """
        tmp_path.mkdir(parents=True, exist_ok=True)
        images = []
        for index in range(count):
            path = tmp_path / f"large_{index}.png"
            PILImage.new("RGB", (900, 700), color=(index * 10, 60, 90)).save(path)
            images.append(_image(path))
        return images

    @pytest.mark.parametrize("batch_size", [2, 4, 8])
    def test_only_one_decoded_image_is_alive_at_a_time(
        self,
        batch_size: int,
        tracking: _LivenessTrackingProcessor,
        tmp_path: Path,
    ) -> None:
        """The demo corpus cannot expose this; a structural assertion can.

        RFC-024 section 9 allows asserting the structural property instead
        of peak RSS, and this is the stronger of the two: RSS is noisy and
        platform-dependent, while "no full-size decode outlives its own
        preprocessing step" is exactly the invariant that keeps a batch of
        24-megapixel photos from costing gigabytes.
        """
        _adapter().encode_images(self._large_photos(tmp_path, batch_size))

        gc.collect()
        assert tracking.peak_live == 1

    def test_the_batch_tensor_is_fixed_size_regardless_of_source_resolution(
        self, tracking: _LivenessTrackingProcessor, tmp_path: Path
    ) -> None:
        """What accumulates across a batch is the small tensor, not the photo.

        Preprocessing collapses each decoded image to the model's fixed
        input size, so the assembled batch grows with the batch *count*
        and not with the source resolution -- the property that makes a
        batch of large photos affordable at all.
        """
        small = self._large_photos(tmp_path / "small", 3)
        large = self._large_photos(tmp_path / "large", 3)

        _adapter().encode_images(small)
        _adapter().encode_images(large)

        assert tracking.peak_live == 1
