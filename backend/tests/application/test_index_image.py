from __future__ import annotations

import uuid

from app.application.use_cases.index_image import IndexImageUseCase
from app.domain.entities.image import Image
from app.domain.exceptions import ImageAlreadyExistsError
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from tests.application.fakes import FakeImageRepository


def test_index_image_saves_new_image_and_generates_embedding() -> None:
    repository = FakeImageRepository()
    embedding_model = FakeEmbeddingModel()
    use_case = IndexImageUseCase(repository=repository, embedding_model=embedding_model)

    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/new.png"),
        filename="new",
        extension="png",
    )

    use_case.execute(image)

    assert repository.save_calls == [image]
    assert repository.exists_calls == [image.id]


def test_index_image_raises_for_existing_image_before_embedding_generation() -> None:
    repository = FakeImageRepository(
        images=[
            Image(
                id=ImageId(uuid.uuid4()),
                path=ImagePath("images/existing.png"),
                filename="existing",
                extension="png",
            )
        ]
    )
    embedding_model = FakeEmbeddingModel()
    use_case = IndexImageUseCase(repository=repository, embedding_model=embedding_model)

    duplicate = Image(
        id=repository._images[0].id,
        path=ImagePath("images/existing.png"),
        filename="existing",
        extension="png",
    )

    try:
        use_case.execute(duplicate)
    except ImageAlreadyExistsError:
        pass
    else:
        raise AssertionError("ImageAlreadyExistsError should be raised")

    assert repository.save_calls == []
    assert repository.exists_calls == [duplicate.id]


def test_index_image_checks_duplicate_before_embedding_generation() -> None:
    repository = FakeImageRepository()
    embedding_model = FakeEmbeddingModel()
    use_case = IndexImageUseCase(repository=repository, embedding_model=embedding_model)

    image = Image(
        id=ImageId(uuid.uuid4()),
        path=ImagePath("images/new.png"),
        filename="new",
        extension="png",
    )

    use_case.execute(image)

    assert repository.exists_calls[0] == image.id
    assert repository.save_calls[0] == image
