from __future__ import annotations

import uuid

from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import FakeImageRepository


def test_search_images_encodes_query_and_returns_repository_results() -> None:
    repository = FakeImageRepository(
        images=[
            Image(
                id=ImageId(uuid.uuid4()),
                path=ImagePath("images/one.png"),
                filename="one",
                extension="png",
            ),
            Image(
                id=ImageId(uuid.uuid4()),
                path=ImagePath("images/two.png"),
                filename="two",
                extension="png",
            ),
        ]
    )
    embedding_model = FakeEmbeddingModel()
    use_case = SearchImagesUseCase(
        repository=repository, embedding_model=embedding_model
    )

    results = use_case.execute("cat")

    assert repository.list_calls == 1
    assert results == repository._images
