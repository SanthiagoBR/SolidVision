"""Request and response shapes for the device API (RFC-031).

Every path published here is **device-relative**, with `/` on every
platform, the form RFC-027 made the only durable one and RFC-030 already
uses for images -- a JSON client should not have to know which operating
system answered in order to split a path into parts.

The one exception is `mount_point`, which is an absolute location and is
in the response on purpose: it is what "plugged in at F:" means, it is
true only for this instant, and it is exactly what the database refuses
to hold (RFC-027 section 4). It is rendered posix-style for the same
reason as everything else here.

**No field is a percentage, and none is a count of files on the disk.**
`indexed_images`, `last_scan_file_count` and `last_scan_at` travel as
three separate values so that whoever renders the ratio cannot do it
without the date in hand (RFC-031 section 4.2); per folder there is no
denominator at all, so there is no ratio to render (section 8.2).
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.application.use_cases.describe_devices import DeviceSummary
from app.application.use_cases.list_device_folders import DeviceFolders, FolderListing
from app.application.use_cases.list_mounted_volumes import CatalogedVolume
from app.domain.entities.indexing_job import JobStatus
from app.domain.value_objects.folder_state import FolderState
from app.presentation.schemas.job_schema import JobSchema


class RegisterDeviceRequestSchema(BaseModel):
    r"""What a client sends to register a disk it saw in `GET /volumes`.

    **There is no path field, and there must never be one.** The identity
    is an opaque key the server minted; the server looks it up in an
    enumeration it just performed and uses *its own* mount point as the
    root. A client never composes a location, and an identity a client
    invented resolves to nothing at all -- which is the difference between
    this and accepting `{"path": "D:\\fotos"}`, and the whole of RFC-031
    section 5.1. `test_device_registration_takes_no_path.py` asserts the
    field set here is exactly these three.

    Both identity fields are plain `str` rather than `VolumeIdentity` and
    `VolumeKind`, and that is deliberate. Declaring `volume_kind:
    VolumeKind` would have FastAPI refuse an unknown value with a `422`
    before the use case ever runs, and the `400
    InvalidVolumeIdentityError` RFC-031 section 6 specifies would become
    unreachable. It is the argument `CreateJobRequestSchema` already
    makes for `scopes`: what makes a value legal is a domain rule, and
    answering it in Presentation produces a 422 where a 400 with a
    readable message belongs.

    Extra fields are **ignored**, which is Pydantic's default and is left
    alone here. A client that smuggles in a `path` gets a successful
    registration of the volume it named and no path is opened, which is
    what the guard test asserts. Only `RenameDeviceRequestSchema` forbids
    extras, and for a reason specific to it.
    """

    volume_identity: str = Field(
        description=(
            "The opaque identity from GET /api/v1/volumes, sent back "
            "unchanged. Never parsed, never composed into a path."
        )
    )
    volume_kind: str = Field(
        description=(
            "Which platform's identifier scheme the identity came from, "
            "e.g. 'windows-volume-guid'. An unknown kind is a 400."
        )
    )
    label: str = Field(
        default="",
        description=(
            "What to call this disk -- 'HD2'. Optional: omitted, the "
            "device keeps the name it has, or falls back to the volume's "
            "own filesystem label and then to the identity. Sent on a "
            "second registration, it is applied, exactly as PATCH would."
        ),
    )


class RenameDeviceRequestSchema(BaseModel):
    """What a client sends to rename a disk. Only the label, and strictly.

    **`extra="forbid"`, the only schema in the project that has it.** The
    default is `extra="ignore"`, and the default is wrong here in a way
    that is invisible: a client sending `last_scan_file_count` would get
    a `200`, believe it had edited the declared denominator of the "%
    indexed" figure, and have been silently ignored. A refusal is the
    only honest answer, because the belief the request encodes is false
    (RFC-031 section 7).

    It is not being added to the other schemas of this RFC. This is the
    route where a field a client might plausibly send is a field this
    system deliberately does not let anyone write, and that is what makes
    silence dangerous here and unremarkable elsewhere.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        description="The new name for this disk. The only editable field.",
    )

    @field_validator("label")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        """Trim the label and refuse one that was nothing but spaces.

        `min_length=1` alone accepts `"   "`, and a disk named with three
        spaces is a sidebar row with a blank line in it. Refused as a
        `422` rather than raised as a domain error: unlike an unknown
        `volume_kind`, whose legality is a rule this system owns and
        already has an error for, there is no domain rule about labels
        anywhere -- a label with no characters in it is a malformed body,
        which is precisely what 422 means.
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("label must not be blank")
        return trimmed


class ActiveJobSchema(BaseModel):
    """The job currently holding a device, as a sidebar needs it.

    Two fields, because two is what the sidebar draws: something is
    happening, and whether it has started. A client that wants counters
    polls `GET /api/v1/jobs/{id}`, which is the route that publishes
    them, rather than having them duplicated into every device list
    (RFC-031 section 4).
    """

    id: UUID = Field(description="The job holding this device")
    status: JobStatus = Field(description="pending (queued) or running")


class DeviceDetailSchema(BaseModel):
    """One device, resolved against the moment the request arrived."""

    id: UUID = Field(description="Identifier of this device")
    label: str = Field(description="The user's name for this disk -- 'HD2'")
    filesystem_label: str | None = Field(
        description=(
            "The name the volume reports for itself. Informative, "
            "frequently empty or 'Untitled', and never an identifier."
        )
    )
    volume_identity: str = Field(
        description=(
            "The platform-stable identity this device is derived from. "
            "Opaque: compare it for equality, never parse it."
        )
    )
    volume_kind: str = Field(
        description="Which platform's identifier scheme the identity came from"
    )
    connected: bool = Field(
        description=(
            "Whether the disk was mounted when this request was served. "
            "**Resolved per request and never stored** -- it is false from "
            "the instant a cable is pulled, and nothing would correct a "
            "stored copy. false is an answer, not an error: the system "
            "still knows every photo on this disk."
        )
    )
    mount_point: str | None = Field(
        description=(
            "Where the disk is attached right now -- 'F:/' -- or null when "
            "it is not. Absent from the database on purpose: a drive "
            "letter is a function of mount order."
        )
    )
    total_bytes: int | None = Field(description="The volume's capacity, if known")
    first_seen_at: datetime.datetime | None = Field(
        description="When this disk was first registered"
    )
    last_seen_at: datetime.datetime | None = Field(
        description=(
            "When it was last observed plugged in. History, not state -- "
            "it never answers 'is it connected now'."
        )
    )
    last_scan_at: datetime.datetime | None = Field(
        description=(
            "When the last scan counted the files on this disk. **Render "
            "this wherever you render a percentage**: the count below is "
            "the declared denominator and it ages."
        )
    )
    last_scan_file_count: int | None = Field(
        description=(
            "How many supported files the last scan found. The denominator "
            "of '% indexed', and only meaningful beside last_scan_at."
        )
    )
    indexed_images: int = Field(
        description=(
            "How many images of this device are in the index. The "
            "numerator. **There is no percent_indexed field**, so that no "
            "client can show the ratio without having held the scan date "
            "(RFC-031 section 4.2)."
        )
    )
    active_job: ActiveJobSchema | None = Field(
        description="The pending or running job holding this disk, if any"
    )

    @classmethod
    def from_summary(cls, summary: DeviceSummary) -> DeviceDetailSchema:
        """Shape a resolved device for the wire.

        A classmethod rather than `from_attributes`, following
        `JobSchema.from_domain`: the summary nests the entity, and
        spelling the mapping out keeps the wire shape a decision rather
        than a reflection of whatever the dataclass happens to hold.

        `mount_point` is rendered with `as_posix()` so the response says
        `F:/` rather than `F:\\`. Every other path in this API is
        `/`-separated and a client should not have to special-case one
        field for the server's platform.
        """
        device = summary.device
        return cls(
            id=device.id.value,
            label=device.label,
            filesystem_label=device.filesystem_label,
            volume_identity=device.volume_identity.value,
            volume_kind=device.volume_identity.kind.value,
            connected=summary.connected,
            mount_point=(
                summary.mount_point.as_posix()
                if summary.mount_point is not None
                else None
            ),
            total_bytes=device.total_bytes,
            first_seen_at=device.first_seen_at,
            last_seen_at=device.last_seen_at,
            last_scan_at=device.last_scan_at,
            last_scan_file_count=device.last_scan_file_count,
            indexed_images=summary.indexed_images,
            active_job=(
                ActiveJobSchema(
                    id=summary.active_job.id.value, status=summary.active_job.status
                )
                if summary.active_job is not None
                else None
            ),
        )


class DeviceListSchema(BaseModel):
    """Every device the system knows, sorted by label."""

    devices: list[DeviceDetailSchema] = Field(
        description="Known devices, connected and not, ordered by label"
    )

    @classmethod
    def from_summaries(cls, summaries: list[DeviceSummary]) -> DeviceListSchema:
        return cls(
            devices=[DeviceDetailSchema.from_summary(summary) for summary in summaries]
        )


class VolumeSchema(BaseModel):
    """One volume attached to this machine right now."""

    volume_identity: str = Field(
        description=(
            "The identity to send back to POST /api/v1/devices. Opaque: it "
            "is a key, not a location, and the server never accepts a path."
        )
    )
    volume_kind: str = Field(
        description="Which platform's identifier scheme this identity came from"
    )
    mount_point: str = Field(description="Where it is attached right now -- 'G:/'")
    filesystem_label: str | None = Field(
        description="The name the volume reports for itself, if any"
    )
    total_bytes: int | None = Field(description="The volume's capacity, if known")
    device_id: str | None = Field(
        description=(
            "The device this volume already is, or null when this system "
            "has never seen it. Non-null is how an 'add a device' screen "
            "shows a disk as already added instead of letting a user "
            "register it twice to no effect (RFC-031 section 6.1)."
        )
    )

    @classmethod
    def from_cataloged(cls, entry: CatalogedVolume) -> VolumeSchema:
        return cls(
            volume_identity=entry.volume.identity.value,
            volume_kind=entry.volume.identity.kind.value,
            mount_point=entry.volume.mount_point.as_posix(),
            filesystem_label=entry.volume.filesystem_label,
            total_bytes=entry.volume.total_bytes,
            device_id=str(entry.device_id) if entry.device_id is not None else None,
        )


class VolumeListSchema(BaseModel):
    """What is plugged into this machine, ordered by mount point."""

    volumes: list[VolumeSchema] = Field(
        description=(
            "Mounted volumes, registered or not. Volumes the platform "
            "refuses to name -- an empty optical drive, a locked BitLocker "
            "volume -- are skipped rather than reported as errors."
        )
    )

    @classmethod
    def from_cataloged(cls, entries: list[CatalogedVolume]) -> VolumeListSchema:
        return cls(volumes=[VolumeSchema.from_cataloged(entry) for entry in entries])


class FolderSchema(BaseModel):
    """One folder inside a device, and what this system knows about it."""

    name: str = Field(description="The folder's own name, as the filesystem spells it")
    path: str = Field(
        description=(
            "Device-relative path, '/'-separated. **This is the exact "
            "string to send in scopes[] on POST /api/v1/jobs** -- it is "
            "the filesystem's own spelling, not whatever spelling was "
            "asked for, because the job route compares path parts exactly."
        )
    )
    has_children: bool = Field(
        description=(
            "Whether this folder holds folders of its own -- draw an expand "
            "arrow only when true"
        )
    )
    indexed_images: int = Field(
        description=(
            "How many images anywhere under this folder are in the index. "
            "An exact count, and **not** a share of anything: there is no "
            "denominator per folder, because obtaining one would mean "
            "walking the subtree (RFC-031 section 2.3). Render '1,204 "
            "indexed', never a percentage."
        )
    )
    state: FolderState = Field(
        description=(
            "indexing, queued, partial, indexed or never_indexed, derived "
            "from the device's job history. partial means some of it was "
            "walked and nothing claims all of it was -- read `job` for "
            "where it stopped."
        )
    )
    job: JobSchema | None = Field(
        description=(
            "The job this state was read from, or null when no job "
            "explains it. For a cancelled or failed one, "
            "last_processed_relative_path is where the work stopped."
        )
    )

    @classmethod
    def from_listing(cls, listing: FolderListing) -> FolderSchema:
        return cls(
            name=listing.name,
            path=str(listing.scope),
            has_children=listing.has_children,
            indexed_images=listing.indexed_images,
            state=listing.state,
            job=(
                JobSchema.from_domain(listing.job) if listing.job is not None else None
            ),
        )


class FolderListSchema(BaseModel):
    """One level of one device's folder tree.

    `path` and `parent` are both canonical spellings and both
    `/`-separated, and `""` means the device root in either of them. The
    root's parent is the root, so a client walking upwards stops there
    rather than falling off.
    """

    device_id: UUID = Field(description="The device these folders are on")
    path: str = Field(
        description=(
            "The folder that was listed, as the filesystem spells it. "
            "Empty means the device root."
        )
    )
    parent: str = Field(
        description="One level up; empty at the root, and the root's parent is itself"
    )
    folders: list[FolderSchema] = Field(
        description=(
            "The folders directly inside `path`, sorted by name. Files are "
            "not listed: what goes back in scopes[] is a folder. Not "
            "paginated -- a folder with thousands of subfolders returns all "
            "of them, a declared debt (RFC-031 section 8.3)."
        )
    )

    @classmethod
    def from_domain(cls, folders: DeviceFolders) -> FolderListSchema:
        return cls(
            device_id=folders.device.id.value,
            path=str(folders.scope),
            parent=str(folders.parent),
            folders=[FolderSchema.from_listing(listing) for listing in folders.folders],
        )
