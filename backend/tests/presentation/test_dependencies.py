from __future__ import annotations

import uuid

from app.application.use_cases.index_image import IndexImageUseCase
from app.application.use_cases.search_images import SearchImagesUseCase
from app.domain.entities.image import Image
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.presentation.dependencies import (
    get_embedding_model,
    get_image_repository,
    get_index_image_use_case,
    get_search_images_use_case,
)


def test_dependency_providers_return_shared_instances() -> None:
    assert get_image_repository() is get_image_repository()
    assert get_embedding_model() is get_embedding_model()


def test_dependency_providers_compose_use_cases() -> None:
    assert isinstance(get_index_image_use_case(), IndexImageUseCase)
    assert isinstance(get_search_images_use_case(), SearchImagesUseCase)


def test_use_cases_share_the_same_repository_instance() -> None:
    repository = get_image_repository()
    index_use_case = get_index_image_use_case()
    search_use_case = get_search_images_use_case()

    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/shared.png"),
        filename="shared",
        extension="png",
    )

    index_use_case.execute(image)
    results = search_use_case.execute("cat")

    assert repository.exists(image.id) is True
    assert results == [image]
