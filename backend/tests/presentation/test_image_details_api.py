"""`GET /api/v1/images/{id}` and `GET /api/v1/images/{id}/thumbnail` (RFC-030).

Mostly level 1 of RFC-026's three: the real routes and real use cases over
in-memory fakes, a real on-disk thumbnail cache under `tmp_path`, and a
`FakeDeviceLocator` standing in for the volume enumeration. One class at the
end is level 2, over PostgreSQL and the production wiring, because a 404
that only works against a fake repository is not a 404.

The conditional-request tests count calls into the thumbnail store rather
than timing responses: "a 304 reads no bytes" is a claim about what was
touched, and a fast response proves nothing about that.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy.orm import Session
from tests.application.fakes import (
    FakeDeviceLocator,
    FakeDeviceRepository,
    FakeImageRepository,
)
from tests.conftest import TEST_DEVICE_ID, make_test_device

from app.application.use_cases.get_image_details import GetImageDetailsUseCase
from app.application.use_cases.get_thumbnail import GetThumbnailUseCase
from app.application.use_cases.index_or_update_images import IndexOrUpdateImagesUseCase
from app.application.use_cases.indexing_plan import IndexCandidate
from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_record import IndexingRecord
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.database.models.image_model import EMBEDDING_DIMENSION
from app.infrastructure.filesystem.image_identity import compute_image_id
from app.infrastructure.filesystem.sha256_content_hasher import Sha256ContentHasher
from app.infrastructure.filesystem.thumbnail_generator import (
    PillowThumbnailGenerator,
)
from app.infrastructure.filesystem.thumbnail_store import FilesystemThumbnailStore
from app.infrastructure.persistence.postgres_image_repository import (
    PostgresImageRepository,
)
from app.infrastructure.persistence.session import get_db
from app.presentation.api import app
from app.presentation.dependencies import (
    get_image_details_use_case,
    get_thumbnail_use_case,
)

RELATIVE = "fotos/2018/junho/DJI_0042.JPG"
HASH = "a" * 64


class CountingStore(FilesystemThumbnailStore):
    """The real store, counting how often anything asks it for a file."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.locate_calls: list[str] = []

    def locate(self, location: str) -> Path | None:
        self.locate_calls.append(location)
        return super().locate(location)


class World:
    """One disk, its images, and the three things the routes are composed of."""

    def __init__(self, tmp_path: Path) -> None:
        self.mount = tmp_path / "disk"
        self.mount.mkdir()
        self.images = FakeImageRepository()
        self.locator = FakeDeviceLocator()
        self.store = CountingStore(tmp_path / "cache")
        self.device = make_test_device("HD3")

    def resolver(self) -> ResolveImageLocationUseCase:
        return ResolveImageLocationUseCase(
            FakeDeviceRepository([self.device]), self.locator
        )

    def index(
        self,
        relative: str = RELATIVE,
        content_hash: str | None = HASH,
        thumbnail: bytes | None = b"\xff\xd8 a jpeg",
    ) -> Image:
        image = Image(
            id=compute_image_id(TEST_DEVICE_ID, ImagePath(relative)),
            device_id=TEST_DEVICE_ID,
            relative_path=ImagePath(relative),
            filename=Path(relative).stem,
            extension="jpg",
            captured_at=datetime.datetime(2018, 6, 12, 14, 30),
            capture_source=CaptureSource.EXIF_ORIGINAL,
        )
        self.images.save_indexed(
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.1] * 8),
                file_size=1,
                file_modified_at=None,
                content_hash=content_hash,
                thumbnail_path=(
                    self.store.save(image.id, thumbnail)
                    if thumbnail is not None
                    else None
                ),
            )
        )
        return image


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    app.dependency_overrides[get_image_details_use_case] = lambda: (
        GetImageDetailsUseCase(world.images, world.resolver())
    )
    app.dependency_overrides[get_thumbnail_use_case] = lambda: GetThumbnailUseCase(
        world.images, world.store
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def details_url(image_id: object) -> str:
    return f"/api/v1/images/{image_id}"


def thumbnail_url(image_id: object) -> str:
    return f"/api/v1/images/{image_id}/thumbnail"


class TestImageDetails:
    def test_an_unknown_id_is_a_404(self, client: TestClient) -> None:
        """Through the re-based `ImageNotFoundError`, not a handler of its own."""
        response = client.get(details_url(uuid.uuid4()))

        assert response.status_code == 404
        assert "No indexed image" in response.json()["detail"]

    def test_a_malformed_id_is_a_422(self, client: TestClient) -> None:
        assert client.get(details_url("not-a-uuid")).status_code == 422

    def test_a_disconnected_disk_is_a_200_that_names_the_disk(
        self, client: TestClient, world: World
    ) -> None:
        """RFC-030 section 4.2: the row exists, and the row is what this describes."""
        image = world.index()

        response = client.get(details_url(image.id.value))

        assert response.status_code == 200
        assert response.json() == {
            "id": str(image.id.value),
            "filename": "DJI_0042",
            "captured_at": "2018-06-12T14:30:00",
            "capture_source": "exif_original",
            "device": {
                "id": str(TEST_DEVICE_ID.value),
                "label": "HD3",
                "connected": False,
            },
            "relative_path": RELATIVE,
            "absolute_path": None,
        }

    def test_a_connected_disk_adds_the_absolute_path(
        self, client: TestClient, world: World
    ) -> None:
        image = world.index()
        world.locator.connect(TEST_DEVICE_ID, world.mount)

        body = client.get(details_url(image.id.value)).json()

        assert body["device"]["connected"] is True
        assert body["absolute_path"] == (world.mount / RELATIVE).as_posix()
        assert "similarity" not in body

    def test_search_is_still_search(self, client: TestClient) -> None:
        """`/search` is declared first; `/{image_id}` must not swallow it."""
        response = client.get("/api/v1/images/search")

        assert response.status_code == 422
        assert any(
            error["loc"] == ["query", "q"] for error in response.json()["detail"]
        )


class TestThumbnail:
    def test_it_is_served_with_the_photos_disk_unplugged(
        self, client: TestClient, world: World
    ) -> None:
        """The premise of RFC-030 section 4.1: the only way to *see* a drawer photo."""
        image = world.index(thumbnail=b"\xff\xd8 the picture")
        assert world.locator.mount_point(world.device) is None

        response = client.get(thumbnail_url(image.id.value))

        assert response.status_code == 200
        assert response.content == b"\xff\xd8 the picture"
        assert response.headers["content-type"] == "image/jpeg"

    def test_the_etag_is_the_content_hash_and_the_cache_must_revalidate(
        self, client: TestClient, world: World
    ) -> None:
        """`no-cache`, never a year of freshness -- see the route's docstring.

        With `max-age=31536000` a browser serves its copy without asking,
        so the `ETag` is never sent back and an overwritten photo keeps its
        old thumbnail for a year. That is the `immutable` bug RFC-030
        section 7.2 corrected, reintroduced by the correction.
        """
        image = world.index()

        response = client.get(thumbnail_url(image.id.value))

        assert response.headers["etag"] == f'"{HASH}"'
        directives = {
            directive.strip()
            for directive in response.headers["cache-control"].split(",")
        }
        assert "no-cache" in directives
        assert "immutable" not in directives
        assert not any(directive.startswith("max-age") for directive in directives)

    def test_a_current_if_none_match_is_a_304_with_no_body_and_no_file_read(
        self, client: TestClient, world: World
    ) -> None:
        image = world.index()

        response = client.get(
            thumbnail_url(image.id.value), headers={"If-None-Match": f'"{HASH}"'}
        )

        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["etag"] == f'"{HASH}"'
        assert world.store.locate_calls == []

    @pytest.mark.parametrize(
        "header",
        [f'W/"{HASH}"', f'"{"b" * 64}", "{HASH}"', f'  "{HASH}"  ', HASH],
        ids=["weak", "list", "padded", "unquoted"],
    )
    def test_if_none_match_is_compared_the_way_http_says(
        self, client: TestClient, world: World, header: str
    ) -> None:
        image = world.index()

        response = client.get(
            thumbnail_url(image.id.value), headers={"If-None-Match": header}
        )

        assert response.status_code == 304

    def test_a_stale_if_none_match_gets_the_file(
        self, client: TestClient, world: World
    ) -> None:
        image = world.index()

        response = client.get(
            thumbnail_url(image.id.value), headers={"If-None-Match": f'"{"b" * 64}"'}
        )

        assert response.status_code == 200
        assert response.content
        assert len(world.store.locate_calls) == 1

    def test_an_image_without_a_thumbnail_is_a_404_for_a_placeholder(
        self, client: TestClient, world: World
    ) -> None:
        image = world.index(thumbnail=None)

        assert client.get(thumbnail_url(image.id.value)).status_code == 404

    def test_an_unknown_image_is_a_404(self, client: TestClient) -> None:
        assert client.get(thumbnail_url(uuid.uuid4())).status_code == 404

    def test_a_thumbnail_missing_from_the_cache_is_a_404_not_a_410(
        self, client: TestClient, world: World
    ) -> None:
        """410 is for the user's photo; the cache is the application's own."""
        image = world.index()
        metadata = world.images.get_index_metadata(image.id)
        assert metadata is not None and metadata.thumbnail_path is not None
        located = world.store.locate(metadata.thumbnail_path)
        assert located is not None
        located.unlink()

        assert client.get(thumbnail_url(image.id.value)).status_code == 404

    def test_a_row_without_a_content_hash_is_served_uncacheable(
        self, client: TestClient, world: World
    ) -> None:
        """No version to validate against, so no validator of ours and no store."""
        image = world.index(content_hash=None)

        response = client.get(thumbnail_url(image.id.value))

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers.get("etag") != '"None"'


class TestReprocessingChangesTheEtag:
    """RFC-030 section 7.2's correction, end to end through the real pipeline.

    A photo overwritten in place keeps its path, and therefore its id. The
    content hash changes, the thumbnail is regenerated under the same id,
    and the `ETag` must follow -- or a client revalidating its cached copy
    would be told the old picture is current.
    """

    def test_new_bytes_at_the_same_path_mean_a_new_etag_and_a_new_picture(
        self, client: TestClient, world: World
    ) -> None:
        photo = world.mount / "DJI_0042.JPG"
        indexer = IndexOrUpdateImagesUseCase(
            repository=world.images,
            embedding_model=FakeEmbeddingModel(),
            content_hasher=Sha256ContentHasher(),
            batch_size=4,
            metadata_prefetch_size=8,
            thumbnail_writer=ThumbnailWriter(
                PillowThumbnailGenerator(), world.store, 64
            ),
        )

        def index_photo(colour: str, size: int) -> ImageId:
            PILImage.new("RGB", (160, 120), colour).save(photo, "JPEG")
            relative = ImagePath("DJI_0042.JPG")
            image = Image(
                id=compute_image_id(TEST_DEVICE_ID, relative),
                device_id=TEST_DEVICE_ID,
                relative_path=relative,
                filename="DJI_0042",
                extension="jpg",
                absolute_path=ImagePath(photo),
            )
            indexer.execute(
                [IndexCandidate(image=image, file_size=size, file_modified_at=None)]
            )
            return image.id

        first_id = index_photo("red", size=1)
        first = client.get(thumbnail_url(first_id.value))

        second_id = index_photo("blue", size=2)
        revalidated = client.get(
            thumbnail_url(second_id.value),
            headers={"If-None-Match": first.headers["etag"]},
        )

        assert first_id == second_id
        assert first.status_code == 200
        assert revalidated.status_code == 200
        assert revalidated.headers["etag"] != first.headers["etag"]
        assert revalidated.content != first.content

        current = client.get(
            thumbnail_url(second_id.value),
            headers={"If-None-Match": revalidated.headers["etag"]},
        )
        assert current.status_code == 304


class TestAgainstPostgres:
    """Level 2: the production wiring, over a real session."""

    @pytest.fixture
    def db_client(self, db_session: Session) -> Iterator[TestClient]:
        app.dependency_overrides[get_db] = lambda: db_session
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def test_an_unknown_id_is_a_404_through_the_real_graph(
        self, db_client: TestClient
    ) -> None:
        assert db_client.get(details_url(uuid.uuid4())).status_code == 404

    def test_a_row_on_a_disk_no_machine_has_is_a_200_disconnected(
        self, db_client: TestClient, db_session: Session
    ) -> None:
        """The test device's volume GUID is one no real disk carries.

        So the real `MountedDeviceLocator` enumerates this machine's volumes
        and correctly finds it nowhere -- which is the drawer case, answered
        by the production code path.
        """
        relative = ImagePath(f"fotos/{uuid.uuid4().hex}.jpg")
        image = Image(
            id=compute_image_id(TEST_DEVICE_ID, relative),
            device_id=TEST_DEVICE_ID,
            relative_path=relative,
            filename="x",
            extension="jpg",
        )
        PostgresImageRepository(db_session).save_indexed(
            IndexingRecord(
                image=image,
                embedding=EmbeddingVector([0.1] * EMBEDDING_DIMENSION),
                file_size=1,
                file_modified_at=None,
            )
        )

        response = db_client.get(details_url(image.id.value))

        assert response.status_code == 200
        body = response.json()
        assert body["device"]["connected"] is False
        assert body["absolute_path"] is None
        assert body["relative_path"] == str(relative)

    def test_a_row_without_a_thumbnail_is_a_404_through_the_real_graph(
        self, db_client: TestClient, db_session: Session
    ) -> None:
        relative = ImagePath(f"fotos/{uuid.uuid4().hex}.jpg")
        image_id = compute_image_id(TEST_DEVICE_ID, relative)
        PostgresImageRepository(db_session).save_indexed(
            IndexingRecord(
                image=Image(
                    id=image_id,
                    device_id=TEST_DEVICE_ID,
                    relative_path=relative,
                    filename="x",
                    extension="jpg",
                ),
                embedding=EmbeddingVector([0.1] * EMBEDDING_DIMENSION),
                file_size=1,
                file_modified_at=None,
            )
        )

        assert db_client.get(thumbnail_url(image_id.value)).status_code == 404
