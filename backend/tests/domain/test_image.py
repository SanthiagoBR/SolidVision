from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.domain.entities.image import Image
from app.domain.exceptions import DeviceNotConnectedError
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath

DEVICE_ID = DeviceId(uuid.UUID(int=7))


def _image(**overrides: object) -> Image:
    fields: dict[str, object] = {
        "id": ImageId(uuid.uuid4()),
        "device_id": DEVICE_ID,
        "relative_path": ImagePath("images/example.png"),
        "filename": "example",
        "extension": "png",
    }
    fields.update(overrides)
    return Image(**fields)  # type: ignore[arg-type]


def test_image_stores_required_attributes() -> None:
    image_id = ImageId(uuid.uuid4())
    relative_path = ImagePath("images/example.png")

    image = Image(
        id=image_id,
        device_id=DEVICE_ID,
        relative_path=relative_path,
        filename="example",
        extension="png",
    )

    assert image.id == image_id
    assert image.device_id == DEVICE_ID
    assert image.relative_path == relative_path
    assert image.filename == "example"
    assert image.extension == "png"


def test_image_is_immutable() -> None:
    image = _image()

    with pytest.raises(FrozenInstanceError):
        image.filename = "other"  # type: ignore[misc]


def test_image_compares_by_identity() -> None:
    image_id = ImageId(uuid.uuid4())
    first = _image(id=image_id, relative_path=ImagePath("images/one.png"))
    second = _image(
        id=image_id,
        device_id=DeviceId(uuid.UUID(int=99)),
        relative_path=ImagePath("images/two.png"),
        extension="jpg",
    )

    assert first == second
    assert hash(first) == hash(second)


def test_image_has_no_infrastructure_behavior() -> None:
    image = _image()

    assert not hasattr(image, "database")
    assert not hasattr(image, "session")


def test_image_has_no_embedding_field() -> None:
    image = _image()

    assert not hasattr(image, "embedding")


def test_image_has_no_absolute_path_by_default() -> None:
    """RFC-027: an image knows its device, not where that device is mounted.

    The default has to be `None` rather than something derived from
    `relative_path`, because a relative path interpreted as absolute would
    resolve against the process's working directory -- a plausible-looking
    path that reads the wrong file.
    """
    assert _image().absolute_path is None


def test_require_absolute_path_returns_it_when_the_device_is_connected() -> None:
    resolved = ImagePath("D:/photos/images/example.png")

    image = _image(absolute_path=resolved)

    assert image.require_absolute_path() == resolved


def test_require_absolute_path_raises_when_the_device_is_not_connected() -> None:
    """The honest failure for a photo on a disk in a drawer (RFC-027 2.3).

    Not `FileNotFoundError`: "the file is gone" justifies dropping a row
    and "the disk is unplugged" must never be allowed to.
    """
    with pytest.raises(DeviceNotConnectedError):
        _image().require_absolute_path()


def test_the_unconnected_failure_names_the_device_and_the_file() -> None:
    """An operator has twenty disks; the message has to say which one."""
    with pytest.raises(DeviceNotConnectedError) as excinfo:
        _image().require_absolute_path()

    message = str(excinfo.value)
    assert "images/example.png" in message
    assert str(DEVICE_ID) in message


def test_absolute_path_does_not_participate_in_equality() -> None:
    """It is a fact about this instant, and equality is about the file.

    The same image resolved on a machine where the disk is `D:` and on one
    where it is `F:` is one image. Letting the resolved path into equality
    would reintroduce, in memory, exactly the instability RFC-027 removed
    from the schema.
    """
    image_id = ImageId(uuid.uuid4())
    unmounted = _image(id=image_id)
    mounted = _image(id=image_id, absolute_path=ImagePath("F:/images/example.png"))

    assert unmounted == mounted
    assert hash(unmounted) == hash(mounted)
