"""Response shape for one indexed image, and the disk it is on (RFC-030 section 4).

RFC-026 left this module as a placeholder, and the placeholder had a reason:
publishing *where* a file is was an open question. RFC-030 answered it.

**`relative_path` always, `absolute_path` when the disk is here.** The first
is a stored fact that holds whether or not anything is plugged in, and on its
own it already tells a user most of what they want -- "fotos/2018/junho". The
second is derived per request from a mount point resolved at that moment, and
is `null` exactly when there is nothing to derive it from.

**Neither is an identifier.** `id` is; the paths are for showing and for the
server to act on. No route accepts a path back, and a client that stores one
to send later has nowhere to send it (RFC-030 section 5.1).

RFC-026 gave two arguments against publishing the path. The first -- leaking
the server's filesystem layout -- does not hold for a local-first deployment
where the server's filesystem is the user's own (RFC-030 section 2.1). The
second -- a path is an unstable identifier -- still holds, and is why the
paths are here as display only.
"""

from __future__ import annotations

import datetime
from typing import TypedDict
from uuid import UUID

from pydantic import BaseModel, Field

from app.application.use_cases.resolve_image_location import LocatedImage
from app.domain.value_objects.capture_source import CaptureSource


class DeviceSchema(BaseModel):
    """The disk an image is on, as the user named it, and whether it is here."""

    id: UUID = Field(description="Stable identifier of the device")
    label: str = Field(description="The user's name for the disk, e.g. HD3")
    connected: bool = Field(
        description=(
            "Whether the disk was mounted when this response was built. "
            "Asked of the operating system per request and never stored, so "
            "it can change between two requests for the same image."
        )
    )


class ImageSchema(BaseModel):
    """One indexed image, where it is, and whether it can be reached right now."""

    id: UUID = Field(description="Stable identifier of the image")
    filename: str = Field(description="Name of the file, without its extension")
    captured_at: datetime.datetime | None = Field(
        description=(
            "When the photo was taken, in the camera's local time, with no "
            "time zone and no offset (e.g. 2018-07-14T15:32:05). Null when "
            "unknown."
        )
    )
    capture_source: CaptureSource | None = Field(
        description=(
            "Where captured_at came from: exif_original (the camera clock at "
            "the shot), exif_digitized (when the image was digitised), or "
            "unknown (the file carries no date). Null if the file has not "
            "been examined yet."
        )
    )
    device: DeviceSchema = Field(description="The disk the file is on")
    relative_path: str = Field(
        description=(
            "Where the file is within its disk, with forward slashes, e.g. "
            "fotos/2018/junho/DJI_0042.JPG. Always present, whether or not "
            "the disk is connected. For display: it is not an identifier and "
            "no route accepts it."
        )
    )
    absolute_path: str | None = Field(
        description=(
            "Where the file is on this machine right now, e.g. "
            "F:/fotos/2018/junho/DJI_0042.JPG. Null when the disk is not "
            "connected -- which is an answer, not an error: the file is on "
            "device.label. For display: it is not an identifier and no route "
            "accepts it."
        )
    )

    @classmethod
    def from_located(cls, located: LocatedImage) -> ImageSchema:
        """Shape a located image for the wire, adding nothing.

        Paths go out through `ImagePath.__str__`, which is POSIX-style on
        every platform. `str(Path)` on Windows would publish backslashes,
        and a JSON client would then have to know which operating system
        answered in order to split a path.
        """
        return cls(**image_fields(located))


class ImageFields(TypedDict):
    """The keyword arguments of `ImageSchema`, typed so `**` stays checked."""

    id: UUID
    filename: str
    captured_at: datetime.datetime | None
    capture_source: CaptureSource | None
    device: DeviceSchema
    relative_path: str
    absolute_path: str | None


def image_fields(located: LocatedImage) -> ImageFields:
    """The fields every image-shaped response shares, in one place.

    Shared with `search_schema.py`, whose results are this shape plus a
    score, so the two responses cannot drift into describing one image two
    ways.
    """
    image = located.image
    return {
        "id": image.id.value,
        "filename": image.filename,
        "captured_at": image.captured_at,
        "capture_source": image.capture_source,
        "device": DeviceSchema(
            id=located.device.id.value,
            label=located.device.label,
            connected=located.connected,
        ),
        "relative_path": str(image.relative_path),
        "absolute_path": (
            str(image.absolute_path) if image.absolute_path is not None else None
        ),
    }
