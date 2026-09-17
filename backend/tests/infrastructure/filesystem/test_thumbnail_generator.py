"""The Pillow thumbnail adapter, against files Pillow actually wrote (RFC-030).

Run over the RFC-022 hard cases rather than over a single friendly JPEG,
because each of them breaks a naive `open().resize().save()` in a way a
user would see: a CMYK file inverted, a 16-bit TIFF rendered white, a
transparent PNG on black, a portrait photo lying on its side.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from PIL import Image as PILImage
from tests.conftest import TEST_DEVICE_ID

from app.domain.entities.image import Image
from app.domain.exceptions import DeviceNotConnectedError
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.thumbnail_generator import (
    PillowThumbnailGenerator,
)
from dataset_tools.generators.hard_cases import FAILED, HARD_CASES, INDEXED, generate

GENERATOR = PillowThumbnailGenerator()


def image_at(path: Path, mounted: bool = True) -> Image:
    return Image(
        id=ImageId(uuid.uuid4()),
        device_id=TEST_DEVICE_ID,
        relative_path=ImagePath(path.name),
        filename=path.stem,
        extension=path.suffix.lstrip("."),
        absolute_path=ImagePath(path) if mounted else None,
    )


def decode(data: bytes) -> PILImage.Image:
    picture = PILImage.open(io.BytesIO(data))
    picture.load()
    return picture


@pytest.fixture(scope="module")
def hard_cases_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("hard_cases")
    generate(root)
    return root


class TestShape:
    def test_the_longer_side_is_bounded_and_the_aspect_ratio_kept(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "wide.jpg"
        PILImage.new("RGB", (1600, 900), "teal").save(source, "JPEG")

        picture = decode(GENERATOR.generate(image_at(source), max_edge=512))

        assert picture.format == "JPEG"
        assert picture.mode == "RGB"
        assert picture.size == (512, 288)

    def test_a_small_source_is_never_enlarged(self, tmp_path: Path) -> None:
        source = tmp_path / "tiny.png"
        PILImage.new("RGB", (40, 30), "teal").save(source, "PNG")

        picture = decode(GENERATOR.generate(image_at(source), max_edge=512))

        assert picture.size == (40, 30)

    def test_a_nonsensical_edge_is_refused(self, tmp_path: Path) -> None:
        source = tmp_path / "a.jpg"
        PILImage.new("RGB", (40, 30)).save(source, "JPEG")

        with pytest.raises(ValueError, match="max_edge"):
            GENERATOR.generate(image_at(source), max_edge=0)


class TestHardCases:
    """RFC-022 section 6.2, from the user's side of the screen."""

    @pytest.mark.parametrize(
        "case",
        [
            case
            for case in HARD_CASES
            if case.expect == INDEXED and case.expect_with_pixel_decoding is None
        ],
        ids=lambda case: case.relative_path,
    )
    def test_every_decodable_case_becomes_a_small_rgb_jpeg(
        self, hard_cases_root: Path, case: object
    ) -> None:
        relative_path = getattr(case, "relative_path")
        source = hard_cases_root / relative_path

        picture = decode(GENERATOR.generate(image_at(source), max_edge=32))

        assert picture.format == "JPEG"
        assert picture.mode == "RGB"
        assert max(picture.size) <= 32

    @pytest.mark.parametrize(
        "case",
        [case for case in HARD_CASES if case.expect_with_pixel_decoding == FAILED],
        ids=lambda case: case.relative_path,
    )
    def test_an_undecodable_file_raises_rather_than_rendering_a_placeholder(
        self, hard_cases_root: Path, case: object
    ) -> None:
        """The caller turns this into a thumbnail failure; the image stays indexed."""
        source = hard_cases_root / getattr(case, "relative_path")

        with pytest.raises(OSError):
            GENERATOR.generate(image_at(source), max_edge=32)

    def test_exif_orientation_is_applied(self, hard_cases_root: Path) -> None:
        """Orientation 6 on 64x48 pixels is a portrait photo, and must look like one."""
        picture = decode(
            GENERATOR.generate(
                image_at(hard_cases_root / "exif_rotated.jpg"), max_edge=64
            )
        )

        assert picture.size == (48, 64)
        assert picture.getexif().get(0x0112) is None

    def test_a_sixteen_bit_tiff_is_scaled_rather_than_clipped_to_white(
        self, hard_cases_root: Path
    ) -> None:
        """Pillow's direct conversion clips every value above 255.

        The fixture is a gradient over the whole 16-bit range, so a clipped
        render is nearly all white and a scaled one averages mid-grey.
        """
        picture = decode(
            GENERATOR.generate(
                image_at(hard_cases_root / "sixteen_bit.tiff"), max_edge=64
            )
        )

        grey = picture.convert("L")
        histogram = grey.histogram()
        mean = sum(level * count for level, count in enumerate(histogram)) / sum(
            histogram
        )
        assert 64 < mean < 192

    def test_transparency_is_flattened_onto_white_not_black(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "clear.png"
        PILImage.new("RGBA", (40, 40), (0, 0, 0, 0)).save(source, "PNG")

        picture = decode(GENERATOR.generate(image_at(source), max_edge=40))

        red, green, blue = picture.getpixel((20, 20))  # type: ignore[misc]
        assert min(red, green, blue) > 245


class TestReadingTheFile:
    def test_an_unmounted_device_raises_rather_than_guessing_a_path(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "a.jpg"
        PILImage.new("RGB", (40, 30)).save(source, "JPEG")

        with pytest.raises(DeviceNotConnectedError):
            GENERATOR.generate(image_at(source, mounted=False), max_edge=32)

    def test_the_source_file_is_left_exactly_as_it_was(self, tmp_path: Path) -> None:
        """RFC-028 section 10: the system never writes to the collection."""
        source = tmp_path / "a.jpg"
        PILImage.new("RGB", (400, 300), "olive").save(source, "JPEG")
        before = (source.read_bytes(), source.stat().st_mtime_ns)

        GENERATOR.generate(image_at(source), max_edge=64)

        assert (source.read_bytes(), source.stat().st_mtime_ns) == before
        assert sorted(tmp_path.iterdir()) == [source]
