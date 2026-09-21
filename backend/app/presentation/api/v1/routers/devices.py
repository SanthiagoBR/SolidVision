r"""HTTP entry points for devices, volumes and their folders (RFC-031).

Five routes, and the thing they have in common is what they refuse to
take: **no route in this module accepts a file path, in a body, a query
string, a header or a URL segment.** The property RFC-030 section 5.1
established for `/reveal` survives a route whose entire purpose is to let
a user add a disk, because the direction is inverted -- the server
enumerates the volumes and mints an opaque identity for each one, and the
client hands one back. A client never composes a location, and an
identity it invented resolves to nothing, which is exactly what a path it
invented would not (RFC-031 section 5.1).

`?path=` on the folder listing is the apparent exception and is not one.
It is a **device-relative scope**, refused by `JobScope` before anything
is opened -- no `..`, no drive anchor, no UNC share -- and then resolved
by the locator, which confirms the result is still inside the device
after NTFS junctions have been followed. It is the same validation `POST
/api/v1/jobs` applies, and it has to be: what this route returns is
literally what the client sends back as `scopes[]`.

Synchronous `def`, not `async def`, for the reason RFC-026 section 9
gives and `jobs.py` documents: everything below this line is a blocking
DBAPI call or a blocking filesystem read, and an `async def` would run it
on the event loop and stall every concurrent request, `/health` included.

No `try/except` anywhere. A device that does not exist, a disk in a
drawer, an unknown volume kind and a folder that is not there are domain
errors, and `error_handlers.py` turns each into 404, 409, 400 and 400
without any function here knowing those numbers. RFC-031 adds no new
error base.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.application.use_cases.describe_devices import DescribeDevicesUseCase
from app.application.use_cases.list_device_folders import ListDeviceFoldersUseCase
from app.application.use_cases.list_devices import ListDevicesUseCase
from app.application.use_cases.list_mounted_volumes import ListMountedVolumesUseCase
from app.application.use_cases.register_device import RegisterDeviceUseCase
from app.application.use_cases.rename_device import RenameDeviceUseCase
from app.domain.value_objects.device_id import DeviceId
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import (
    get_describe_devices_use_case,
    get_list_device_folders_use_case,
    get_list_devices_use_case,
    get_list_mounted_volumes_use_case,
    get_register_device_use_case,
    get_rename_device_use_case,
)
from app.presentation.schemas.device_schema import (
    DeviceDetailSchema,
    DeviceListSchema,
    FolderListSchema,
    RegisterDeviceRequestSchema,
    RenameDeviceRequestSchema,
    VolumeListSchema,
)

router = APIRouter(tags=["devices"])
logger = get_logger(__name__)


@router.get(
    "/devices",
    status_code=status.HTTP_200_OK,
    response_model=DeviceListSchema,
    summary="List every known device, with connection resolved right now",
)
def list_devices(
    use_case: ListDevicesUseCase = Depends(get_list_devices_use_case),
) -> DeviceListSchema:
    """Return every disk the system knows, connected or in a drawer.

    `connected` and `mount_point` describe **this instant** and are
    resolved per request from a single enumeration of the mounted
    volumes, however many devices there are. That bound is the design
    (RFC-031 section 4.1): the obvious implementation asks the locator
    once per device, which is right for a page of search hits and is N
    enumerations of the same filesystem here.

    `connected: false` is a normal answer, not a failure. The row exists,
    every photo on that disk is still searchable, and its thumbnails are
    still served (RFC-030 section 4.1) -- what the user is missing is the
    bytes.

    No `percent_indexed`. The response carries `indexed_images`,
    `last_scan_file_count` and `last_scan_at` separately, so that nobody
    can render the ratio without having the scan date in hand
    (ARCHITECTURE.md section 15, RFC-031 section 4.2).
    """
    return DeviceListSchema.from_summaries(use_case.execute())


@router.get(
    "/volumes",
    status_code=status.HTTP_200_OK,
    response_model=VolumeListSchema,
    summary="List the volumes attached to this machine right now",
)
def list_volumes(
    use_case: ListMountedVolumesUseCase = Depends(get_list_mounted_volumes_use_case),
) -> VolumeListSchema:
    """Answer *what is plugged in, and what of it do we already know?*

    The question that precedes registering a disk, and the reason the
    registration below never needs a path: this hands the client an
    opaque identity for each volume, and the client hands one back.

    **Deliberately the expensive route of the five.** Each volume costs
    an extra platform call for its label and capacity, and reading a
    capacity can spin up an external disk that had parked its heads. That
    is why `VolumeCatalog` has two methods and why `GET /devices` above
    uses the cheap one -- folding the detail into the enumeration would
    put this cost in front of every search in the product (RFC-031
    section 4.1).

    Volumes the platform refuses to describe are skipped, not reported as
    errors: an empty optical drive is no reason to stop listing the disks
    that answered (RFC-027 section 7).
    """
    return VolumeListSchema.from_cataloged(use_case.execute())


@router.post(
    "/devices",
    status_code=status.HTTP_201_CREATED,
    response_model=DeviceDetailSchema,
    summary="Register a mounted volume as a device, by identity",
    responses={
        200: {
            "description": (
                "This volume was already registered. Nothing was created; "
                "the label sent, if any, was applied."
            )
        },
        400: {"description": "The volume identity is empty or of an unknown kind"},
        409: {"description": "No volume with that identity is mounted right now"},
    },
)
def register_device(
    request: RegisterDeviceRequestSchema,
    response: Response,
    use_case: RegisterDeviceUseCase = Depends(get_register_device_use_case),
    describer: DescribeDevicesUseCase = Depends(get_describe_devices_use_case),
) -> DeviceDetailSchema:
    r"""Register the volume with this identity, or return the device it already is.

    **201 the first time, 200 the second, and the difference is
    load-bearing.** `DeviceId` is `uuid5` over the volume identity, so
    registering a known disk lands on the existing row: nothing was
    created, and a client counting creations would be counting wrong. It
    is not a 409 either -- a conflict describes a request the current
    state refuses, and this one is not refused, it is already satisfied
    (RFC-031 section 6.1). The use case reports which happened; this
    function only chooses the number.

    A `label` sent on a second registration **is** applied. `save()` is
    an upsert by design, and the alternative -- ignoring it in silence --
    would leave a user renaming a disk with no effect.

    **Neither `response` nor `describer` is client input.** FastAPI
    injects the first so the status can be lowered to 200 and resolves
    the second as a dependency; nothing a client sends can reach either.
    The signature test asserts the parameter list is exactly these four
    names, so a path parameter added later has nowhere to hide.

    The `describer` is composed here rather than inside the registration
    for the reason the search route composes `SearchImagesUseCase` with
    `ResolveImageLocationUseCase`: resolving where a disk is mounted and
    how much of it is indexed is not registering it, and the CLI path
    through `RegisterDeviceUseCase.register()` must stay free of both.
    It is what makes this response say `connected: true` with a real
    mount point, rather than reporting a disk the server has this
    instant found mounted as absent.

    An identity nobody is holding is a 409 naming what to do about it --
    plug the disk in -- and **nothing is written**. An invented identity
    is not a location: it is looked up in a mapping the server produced
    seconds ago, and what is not in it does not exist. A path, by
    contrast, *is* a location whether or not anyone meant it to be, which
    is the whole of RFC-031 section 5.1.
    """
    logger.info("device registration requested: kind=%s", request.volume_kind)
    registered = use_case.execute(
        volume_identity=request.volume_identity,
        volume_kind=request.volume_kind,
        label=request.label,
    )
    if not registered.created:
        response.status_code = status.HTTP_200_OK
    logger.info(
        "device %s registered (created=%s)", registered.device.id, registered.created
    )
    return DeviceDetailSchema.from_summary(describer.execute_one(registered.device))


@router.patch(
    "/devices/{device_id}",
    status_code=status.HTTP_200_OK,
    response_model=DeviceDetailSchema,
    summary="Rename a device",
    responses={404: {"description": "No device has this id"}},
)
def rename_device(
    device_id: UUID,
    request: RenameDeviceRequestSchema,
    use_case: RenameDeviceUseCase = Depends(get_rename_device_use_case),
) -> DeviceDetailSchema:
    """Change the disk's label. Nothing else about a device is editable.

    **Works with the disk in a drawer**, and that is the whole point of
    the rename requiring no bytes: a name is a fact about our table, and
    RFC-027 section 2.3 is explicit that an unplugged disk is a device
    the system knows perfectly well. The response still *reports*
    whether the disk is here, and reporting `false` is as successful an
    outcome as reporting `true`.

    A body carrying anything but `label` is a **422**, not a quietly
    ignored field. `last_scan_file_count` is the plausible thing to send
    and the exact thing that must not be writable: it is the declared
    denominator of "% indexed", so an editable one is an invented number
    with the appearance of a measurement (RFC-031 section 7).

    The response is the shape `GET /devices` returns, resolved the same
    way, so a client can drop it straight into the row it renamed rather
    than re-reading the list to learn whether anything else about the
    disk still holds.
    """
    logger.info("rename requested for device %s", device_id)
    return DeviceDetailSchema.from_summary(
        use_case.execute(DeviceId(device_id), request.label)
    )


@router.get(
    "/devices/{device_id}/folders",
    status_code=status.HTTP_200_OK,
    response_model=FolderListSchema,
    summary="List one level of a device's folders, with each folder's state",
    responses={
        400: {"description": "The path escapes the device, or is not a folder on it"},
        404: {"description": "No device has this id"},
        409: {"description": "The device is not connected"},
    },
)
def list_device_folders(
    device_id: UUID,
    path: str = Query(
        default="",
        description=(
            "Folder to list, relative to the device root, e.g. '2018'. "
            "Omit, or send an empty string, for the root. A drive letter, "
            "a leading separator, a UNC share or any '..' is refused "
            "before anything is opened."
        ),
    ),
    use_case: ListDeviceFoldersUseCase = Depends(get_list_device_folders_use_case),
) -> FolderListSchema:
    """Return the folders directly inside `path`, each with its indexing state.

    **One level per request.** Not a flat first level, which would turn
    "index the wedding" into "index all of 2018" -- five hours of
    inference to reach three hundred photos -- and not the whole tree,
    which costs the size of the disk every time the screen opens
    (RFC-031 section 8.1).

    **The paths that come back are the filesystem's spelling, not the
    one that was asked for.** A request for `?path=FOTOS/2018` against a
    folder called `Fotos/2018` answers `"path": "Fotos/2018"`, because
    that is the string the client will send in `scopes[]` and `POST
    /jobs` compares path parts exactly.

    **Requires the disk to be plugged in: 409, naming it.** Unlike the
    rename above, this one needs bytes, and *which disk to plug in* is
    more useful than being told a folder is not there.

    No count of files on the disk and no percentage per folder. There is
    no denominator, and getting one means walking the subtree -- the read
    RFC-029 section 7.2 measured as dominant on a cold mechanical disk.
    `indexed_images` is exact and is what to render (RFC-031 section
    2.3).

    Not paginated. A folder with thousands of subfolders returns all of
    them; the debt is declared in RFC-031 section 8.3 and measured before
    it is paid.
    """
    return FolderListSchema.from_domain(use_case.execute(DeviceId(device_id), path))
