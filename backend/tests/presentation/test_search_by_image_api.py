"""`POST /api/v1/images/search/by-image` at the HTTP edge (RFC-038 section 4.2).

The real dependencies that read the body and keep the temporary file, over a
`tmp_path` that stands in for `cache\\query-images`; the search use case over
the fake repository and a model that decodes nothing. Every refusal is
followed by a look at the folder: the temporary file must be gone however
the request ended.

The 50 MB ceiling is lowered for the tests that cross it, rather than sending
50 MB through the test client: what is under test is where the line is drawn
and that crossing it is a 413, not the size of the number.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
)
from tests.conftest import TEST_DEVICE_ID, make_test_device

from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.presentation import query_image_upload
from app.presentation.api import app
from app.presentation.dependencies import (
    get_resolve_image_location_use_case,
    get_search_images_use_case,
)
from app.presentation.query_image_upload import get_query_image_directory

URL = "/api/v1/images/search/by-image"


def encoded(format_: str, size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, (120, 160, 90)).save(buffer, format=format_)
    return buffer.getvalue()


JPEG = encoded("JPEG")
PNG = encoded("PNG")


class SeeingModel(FakeEmbeddingModel):
    """The fake model, noting whether the picture was on disk when encoded."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[Path, bool]] = []

    def encode_image(self, image: Image) -> EmbeddingVector:
        assert image.absolute_path is not None
        path = image.absolute_path.value
        self.seen.append((path, path.is_file()))
        return super().encode_image(image)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "query-images"


@pytest.fixture
def model() -> SeeingModel:
    return SeeingModel()


@pytest.fixture
def repository() -> FakeImageRepository:
    repository = FakeImageRepository()
    for index in range(3):
        repository.seed_embedding(
            Image(
                id=ImageId(uuid.uuid4()),
                device_id=TEST_DEVICE_ID,
                relative_path=ImagePath(f"fotos/IMG_{index}.JPG"),
                filename=f"IMG_{index}",
                extension="jpg",
            ),
            EmbeddingVector([0.1 * (index + 1)] + [0.05] * 511),
        )
    return repository


@pytest.fixture
def composed() -> list[str]:
    return []


@pytest.fixture
def client(
    folder: Path,
    model: SeeingModel,
    repository: FakeImageRepository,
    composed: list[str],
) -> Iterator[TestClient]:
    def use_case() -> SearchImagesUseCase:
        composed.append("search")
        return SearchImagesUseCase(
            repository=repository, embedding_model=model, default_limit=10
        )

    app.dependency_overrides[get_query_image_directory] = lambda: folder
    app.dependency_overrides[get_search_images_use_case] = use_case
    app.dependency_overrides[get_resolve_image_location_use_case] = lambda: (
        ResolveImageLocationUseCase(
            device_repository=FakeDeviceRepository([make_test_device("HD3")]),
            device_locator=FakeDeviceLocator(),
        )
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    app.dependency_overrides.clear()


def leftovers(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


class TestTheSearch:
    def test_a_small_jpeg_is_ranked_against_the_index(
        self, client: TestClient, model: SeeingModel, folder: Path
    ) -> None:
        response = client.post(
            URL, content=JPEG, headers={"Content-Type": "image/jpeg"}
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "limit",
            "results",
            "excluded_unknown_date",
            "excluded_unknown_position",
        }
        assert len(body["results"]) == 3
        assert body["results"][0]["device"]["label"] == "HD3"
        ((path, present),) = model.seen
        assert present, "the picture was not on disk when the model read it"
        assert path.parent == folder.resolve()
        assert path.suffix == ".jpg"
        assert leftovers(folder) == []

    def test_a_png_works_and_keeps_its_extension(
        self, client: TestClient, model: SeeingModel
    ) -> None:
        response = client.post(URL, content=PNG, headers={"Content-Type": "image/png"})

        assert response.status_code == 200
        ((path, _),) = model.seen
        assert path.suffix == ".png"

    def test_the_filters_and_the_limit_are_the_searchs(
        self, client: TestClient, repository: FakeImageRepository
    ) -> None:
        response = client.post(
            URL,
            content=JPEG,
            headers={"Content-Type": "image/jpeg"},
            params={"limit": 2, "captured_from": "2018-01-01"},
        )

        body = response.json()
        assert body["limit"] == 2
        assert body["results"] == []
        assert body["excluded_unknown_date"] == 3
        ((_, limit, filters),) = repository.search_similar_calls
        assert limit == 2
        assert filters is not None and filters.captured_between is not None

    def test_a_parameter_on_the_type_is_accepted(self, client: TestClient) -> None:
        response = client.post(
            URL, content=JPEG, headers={"Content-Type": "Image/JPEG; q=1"}
        )

        assert response.status_code == 200


class TestTheRouteExists:
    def test_it_is_a_200_and_never_a_422(self, client: TestClient) -> None:
        """Declared beside `/search`: never read as an image id."""
        response = client.post(
            URL, content=JPEG, headers={"Content-Type": "image/jpeg"}
        )

        assert response.status_code == 200

    def test_the_openapi_document_declares_the_two_types(self) -> None:
        operation = app.openapi()["paths"][URL]["post"]
        content = operation["requestBody"]["content"]

        assert set(content) == {"image/jpeg", "image/png"}
        assert operation["requestBody"]["required"] is True


class TestRefusedByType:
    @pytest.mark.parametrize(
        "media_type",
        [
            "text/plain",
            "multipart/form-data; boundary=x",
            "application/x-www-form-urlencoded",
            "image/webp",
        ],
    )
    def test_another_type_is_a_415(
        self, client: TestClient, folder: Path, media_type: str
    ) -> None:
        response = client.post(URL, content=JPEG, headers={"Content-Type": media_type})

        assert response.status_code == 415
        assert leftovers(folder) == []

    def test_no_type_at_all_is_a_415(self, client: TestClient) -> None:
        response = client.post(URL, content=JPEG, headers={"Content-Type": ""})

        assert response.status_code == 415

    def test_a_png_declared_as_a_jpeg_is_a_415(
        self, client: TestClient, folder: Path
    ) -> None:
        response = client.post(URL, content=PNG, headers={"Content-Type": "image/jpeg"})

        assert response.status_code == 415
        assert leftovers(folder) == []

    def test_a_refusal_never_composes_the_model(
        self, client: TestClient, composed: list[str]
    ) -> None:
        """The body is checked before the use case, whose provider imports torch."""
        client.post(URL, content=b"hello", headers={"Content-Type": "text/plain"})

        assert composed == []


class TestRefusedBySize:
    @pytest.fixture(autouse=True)
    def small_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(query_image_upload, "MAX_QUERY_IMAGE_BYTES", 1024)

    def test_a_declared_length_above_the_ceiling_is_a_413(
        self, client: TestClient, folder: Path
    ) -> None:
        body = JPEG[:3] + b"\0" * 2048

        response = client.post(
            URL, content=body, headers={"Content-Type": "image/jpeg"}
        )

        assert response.status_code == 413
        assert "MB" in response.json()["detail"]
        assert leftovers(folder) == []

    def test_a_body_without_a_length_is_counted_and_cut(
        self, client: TestClient, folder: Path
    ) -> None:
        def chunks() -> Iterator[bytes]:
            yield JPEG[:3]
            for _ in range(8):
                yield b"\0" * 256

        response = client.post(
            URL, content=chunks(), headers={"Content-Type": "image/jpeg"}
        )

        assert response.status_code == 413
        assert leftovers(folder) == []

    def test_the_ceiling_itself_is_allowed_through(self, client: TestClient) -> None:
        """At the ceiling, the next check speaks: these bytes are not a picture."""
        body = JPEG[:3] + b"\0" * (1024 - 3)

        response = client.post(
            URL, content=body, headers={"Content-Type": "image/jpeg"}
        )

        assert response.status_code == 400


class TestRefusedByTheDecoder:
    def test_bytes_that_only_start_like_a_jpeg_are_a_400(
        self, client: TestClient, folder: Path
    ) -> None:
        response = client.post(
            URL,
            content=b"\xff\xd8\xff" + b"not a picture" * 10,
            headers={"Content-Type": "image/jpeg"},
        )

        assert response.status_code == 400
        assert leftovers(folder) == []

    def test_a_truncated_jpeg_is_a_400(self, client: TestClient, folder: Path) -> None:
        whole = encoded("JPEG", (256, 192))

        response = client.post(
            URL,
            content=whole[: len(whole) // 2],
            headers={"Content-Type": "image/jpeg"},
        )

        assert response.status_code == 400
        assert leftovers(folder) == []

    def test_a_picture_above_the_pixel_limit_is_a_400(
        self, client: TestClient, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Image.MAX_IMAGE_PIXELS` lowered: the check is the limit, not 89 MP."""
        monkeypatch.setattr(PILImage, "MAX_IMAGE_PIXELS", 32 * 24 - 1)

        response = client.post(URL, content=PNG, headers={"Content-Type": "image/png"})

        assert response.status_code == 400
        assert "pixels" in response.json()["detail"]
        assert leftovers(folder) == []


class TestTheFolder:
    def test_an_error_in_the_search_still_deletes_the_file(
        self, client: TestClient, model: SeeingModel, folder: Path
    ) -> None:
        def broken(image: Image) -> EmbeddingVector:
            raise RuntimeError("the model broke")

        model.encode_image = broken  # type: ignore[method-assign]

        with pytest.raises(RuntimeError):
            client.post(URL, content=JPEG, headers={"Content-Type": "image/jpeg"})

        assert leftovers(folder) == []

    def test_the_folder_is_emptied_when_the_api_starts(
        self, folder: Path, client: TestClient
    ) -> None:
        """What a process that died left behind; the fixture already started one."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "abandoned.jpg").write_bytes(JPEG)

        with TestClient(app, base_url="http://127.0.0.1"):
            pass

        assert leftovers(folder) == []

    def test_a_missing_folder_is_not_an_error(self, tmp_path: Path) -> None:
        assert query_image_upload.empty_query_image_directory(tmp_path / "none") == 0

    def test_the_default_folder_is_the_layouts(self) -> None:
        from app.infrastructure.config.layout import layout

        assert get_query_image_directory() == (
            layout.data_directory / "cache" / "query-images"
        )
