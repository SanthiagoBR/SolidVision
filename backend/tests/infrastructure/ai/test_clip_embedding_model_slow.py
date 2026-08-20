"""Opt-in tests that run the real CLIP and Marian checkpoints (RFC-023 15).

Every test here is marked `slow` and is deselected by the default `addopts`
in `pyproject.toml`, so `pytest` stays offline, fast, and deterministic. Run
them deliberately:

    pytest -m slow

First execution downloads roughly 600 MB of CLIP weights plus the Marian
translation model, and CPU inference takes about half a second per image on
the machine the bake-off was run on
(`experiments/rfc-023-siglip-bakeoff/clip_small_dim_run_output.log`).

These are the tests that would catch a transformers upgrade changing what
`get_image_features()` returns -- the exact breakage the unit-test stubs
cannot see, because the stubs encode today's contract rather than verify it.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import pytest
from PIL import Image as PILImage

from app.application.use_cases.index_or_update_image import IndexOrUpdateImageUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.clip_embedding_model import ClipEmbeddingModel
from app.infrastructure.config.constants import SUPPORTED_IMAGE_EXTENSIONS
from app.infrastructure.config.settings import settings
from app.infrastructure.filesystem.filesystem_image_provider import (
    FilesystemImageProvider,
)
from app.infrastructure.persistence.in_memory_image_repository import (
    InMemoryImageRepository,
)
from app.infrastructure.workers.indexing_worker import IndexingWorker
from dataset_tools.generators.hard_cases import (
    FAILED,
    HARD_CASES,
    IGNORED,
    INDEXED,
    HardCase,
    generate,
)

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def clip() -> ClipEmbeddingModel:
    """One adapter for the whole module -- loading the checkpoint is the cost."""
    return ClipEmbeddingModel()


def _image(path: Path) -> Image:
    image_path = ImagePath(str(path))
    return Image(
        id=ImageId(uuid.uuid4()),
        path=image_path,
        filename=path.stem,
        extension=path.suffix.lstrip(".").lower(),
    )


def _cosine(left: EmbeddingVector, right: EmbeddingVector) -> float:
    return sum(x * y for x, y in zip(left.values, right.values, strict=True))


def _write_photo(path: Path, color: tuple[int, int, int]) -> Path:
    PILImage.new("RGB", (256, 192), color=color).save(path, "JPEG", quality=95)
    return path


class TestRealCheckpointContract:
    """Pins what the installed transformers actually returns."""

    def test_image_embedding_has_the_configured_dimension(
        self, clip: ClipEmbeddingModel, tmp_path: Path
    ) -> None:
        path = _write_photo(tmp_path / "photo.jpg", (40, 110, 60))

        embedding = clip.encode_image(_image(path))

        assert len(embedding.values) == settings.embedding_dimension == 512

    def test_text_embedding_has_the_configured_dimension(
        self, clip: ClipEmbeddingModel
    ) -> None:
        embedding = clip.encode_text("a rural property with a small lake")

        assert len(embedding.values) == 512

    def test_image_and_text_embeddings_share_one_space(
        self, clip: ClipEmbeddingModel, tmp_path: Path
    ) -> None:
        path = _write_photo(tmp_path / "photo.jpg", (40, 110, 60))

        image_embedding = clip.encode_image(_image(path))
        text_embedding = clip.encode_text("a green field")

        assert len(image_embedding.values) == len(text_embedding.values)
        assert -1.0 <= _cosine(image_embedding, text_embedding) <= 1.0

    @pytest.mark.parametrize("query", ["a lake", "an industrial warehouse"])
    def test_text_embeddings_are_unit_norm(
        self, query: str, clip: ClipEmbeddingModel
    ) -> None:
        embedding = clip.encode_text(query)

        norm = math.sqrt(sum(value * value for value in embedding.values))
        assert norm == pytest.approx(1.0, abs=1e-5)

    def test_image_embeddings_are_unit_norm(
        self, clip: ClipEmbeddingModel, tmp_path: Path
    ) -> None:
        path = _write_photo(tmp_path / "photo.jpg", (200, 60, 60))

        embedding = clip.encode_image(_image(path))

        norm = math.sqrt(sum(value * value for value in embedding.values))
        assert norm == pytest.approx(1.0, abs=1e-5)

    def test_encoding_is_deterministic(
        self, clip: ClipEmbeddingModel, tmp_path: Path
    ) -> None:
        """The same file must always produce the same stored vector."""
        path = _write_photo(tmp_path / "photo.jpg", (10, 10, 200))
        image = _image(path)

        assert clip.encode_image(image).values == clip.encode_image(image).values
        assert clip.encode_text("a lake").values == clip.encode_text("a lake").values

    def test_different_content_produces_different_embeddings(
        self, clip: ClipEmbeddingModel, tmp_path: Path
    ) -> None:
        """The distinction `FakeEmbeddingModel` cannot make: real pixels matter."""
        red_path = _write_photo(tmp_path / "a.jpg", (220, 20, 20))
        blue_path = _write_photo(tmp_path / "b.jpg", (20, 20, 220))

        red = clip.encode_image(_image(red_path))
        blue = clip.encode_image(_image(blue_path))

        assert _cosine(red, blue) < 0.999


class TestRealTranslation:
    def test_a_portuguese_query_is_translated_before_templating(
        self, clip: ClipEmbeddingModel
    ) -> None:
        prompt = clip.build_prompt("propriedade rural com um pequeno lago")

        assert prompt.startswith("a photo of ")
        assert "propriedade" not in prompt
        assert "lake" in prompt.lower()

    def test_an_english_query_is_left_alone(self, clip: ClipEmbeddingModel) -> None:
        query = "rural property with a small lake"

        assert clip.build_prompt(query) == f"a photo of {query}"

    def test_translated_portuguese_lands_near_its_english_equivalent(
        self, clip: ClipEmbeddingModel
    ) -> None:
        """The point of translating at all: one query, one region of the space."""
        portuguese = clip.encode_text("propriedade rural com um pequeno lago")
        english = clip.encode_text("rural property with a small lake")
        unrelated = clip.encode_text("a ceramic coffee cup and saucer")

        assert _cosine(portuguese, english) > _cosine(portuguese, unrelated)


class TestHardCasesWithRealPixelDecoding:
    """Activates the RFC-022 6.2 `expect_with_pixel_decoding` expectations.

    Those expectations were dormant because `FakeEmbeddingModel` hashes the
    id and path and never opens the file, so a structurally broken image
    still indexed cleanly. With a real adapter reading pixels, the two cases
    that declare `expect_with_pixel_decoding=FAILED` must now actually fail
    -- and, just as importantly, every other case must still succeed.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def indexing_run(
        tmp_path_factory: pytest.TempPathFactory,
    ) -> tuple[Path, set[str]]:
        """Index the hard-case corpus once, through the real production path."""
        root = tmp_path_factory.mktemp("hard_cases")
        generate(root)

        repository = InMemoryImageRepository()
        IndexingWorker(
            filesystem_provider=FilesystemImageProvider(
                root, SUPPORTED_IMAGE_EXTENSIONS
            ),
            index_or_update_use_case=IndexOrUpdateImageUseCase(
                repository=repository,
                embedding_model=ClipEmbeddingModel(),
            ),
        ).run()

        return root, {str(image.path) for image in repository.list()}

    @pytest.mark.parametrize(
        "case",
        [case for case in HARD_CASES if case.expect_with_pixel_decoding is None],
        ids=lambda case: case.relative_path,
    )
    def test_cases_unaffected_by_decoding_keep_their_outcome(
        self, case: HardCase, indexing_run: tuple[Path, set[str]]
    ) -> None:
        root, indexed_paths = indexing_run
        path = (root / case.relative_path).as_posix()

        if case.expect == INDEXED:
            assert path in indexed_paths, case.description
        else:
            assert case.expect == IGNORED
            assert path not in indexed_paths, case.description

    @pytest.mark.parametrize(
        "case",
        [case for case in HARD_CASES if case.expect_with_pixel_decoding is not None],
        ids=lambda case: case.relative_path,
    )
    def test_structurally_broken_files_now_fail_to_index(
        self, case: HardCase, indexing_run: tuple[Path, set[str]]
    ) -> None:
        """`zero_byte.jpg` and `truncated.jpg` cannot survive a real decode."""
        root, indexed_paths = indexing_run

        assert case.expect_with_pixel_decoding == FAILED
        assert (root / case.relative_path).as_posix() not in indexed_paths

    def test_one_broken_file_still_does_not_abort_the_run(
        self, indexing_run: tuple[Path, set[str]]
    ) -> None:
        """`IndexingWorker`'s per-file isolation is what makes propagation safe."""
        _, indexed_paths = indexing_run
        expected = sum(
            1
            for case in HARD_CASES
            if (case.expect_with_pixel_decoding or case.expect) == INDEXED
        )

        assert len(indexed_paths) == expected
