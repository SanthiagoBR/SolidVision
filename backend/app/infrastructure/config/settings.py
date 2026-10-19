"""Application configuration: the fields, and where each mode reads them from.

Two modes, read from the disk by `layout.py` and never declared (RFC-034
section 4.2), and one precedence for each (section 5.1), strongest first:

    development                         installed
    1  constructor arguments (tests)    constructor arguments (tests)
    2  the process environment          --
    3  <repository>\\.env                %LOCALAPPDATA%\\SolidVision\\solidvision.env
    4  defaults                         defaults

Each mode reads one file and never the other's. Installed, the process
environment is not a source: there it is whatever the other programs on the
machine left behind, and a `DEBUG=*` meant for one of them would otherwise
stop this one from importing (section 2.3).

The connection to the embedded cluster is in neither column. The engine reads
it from the cluster at every new connection, and it is never a field here
(section 7).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated, Any, ClassVar, NamedTuple, Self

from dotenv import dotenv_values
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.infrastructure.process.supervision import refuse_to_start

from .constants import SUPPORTED_IMAGE_EXTENSIONS
from .layout import Environment, Layout, layout

CONFIG_FILE_ENCODING = "utf-8-sig"
"""UTF-8, with or without a BOM (RFC-034 section 5.3).

Notepad's "UTF-8 with BOM" is the silent case: read as plain `utf-8`, the BOM
becomes part of the first key, and the first line of the file is dropped
without an error. Measured in section 2.4.
"""

PATH_DEFAULTS: dict[str, Callable[[Layout], Path]] = {
    "thumbnail_directory": lambda where: where.thumbnail_directory,
    "log_directory": lambda where: where.log_directory,
    "embedded_postgres_directory": lambda where: where.postgres_directory,
    "embedded_postgres_runtime_directory": lambda where: where.postgres_runtime,
    "model_runtime_directory": lambda where: where.model_runtime,
}
"""Every path setting, and where its default comes from: the layout.

The five are `Path`, and each leaves `Settings` absolute (section 6.3). A
test checks that no other field is a path, and that none of these is ever
relative.
"""

MODE_DEFAULTS: dict[str, Callable[[Layout], Any]] = {
    "warm_up_models": lambda where: where.environment is Environment.INSTALLED,
}
"""The settings whose default depends on the mode rather than on a path.

`warm_up_models` is on installed and off in a checkout (RFC-035 section 8).
"""


def _layout_default(name: str) -> Callable[[], Path]:
    """The default of a path setting, for the layout of the running code."""
    return lambda: PATH_DEFAULTS[name](layout)


def _mode_default(name: str) -> Callable[[], Any]:
    """The default of a mode setting, for the layout of the running code."""
    return lambda: MODE_DEFAULTS[name](layout)


RETIRED_KEYS = {
    "ENVIRONMENT": "the mode is read from the installation itself -- development "
    "when pyproject.toml is beside the code -- and nothing sets it (RFC-034 "
    "section 4.2)",
    "INDEXING_ROOT_PATH": "indexing is per device and scopes (RFC-029), and "
    "nothing reads a root path",
    "EMBEDDING_MODEL": "the model is the lockfile's, materialized in "
    "runtime\\models (RFC-035)",
}
"""Keys that used to be settings, with what replaced each (section 5.4)."""

DATABASE_KEYS = (
    "database_source",
    "database_host",
    "database_port",
    "database_name",
    "database_user",
    "database_password",
)
"""Without effect in the user's file: installed, the connection is always the
embedded cluster's (section 7.1)."""


class DatabaseSource(StrEnum):
    """Where the database connection comes from (RFC-034 section 7.1)."""

    EXTERNAL = "external"
    EMBEDDED = "embedded"


class ConfigurationError(Exception):
    """The configuration cannot be used, and the process must not start with it.

    One line: the file, the key and the problem (RFC-034 section 5.5). Starting
    with the default in place of an invalid value would be worse -- whoever
    pointed `THUMBNAIL_DIRECTORY` at another disk and mistyped the path would
    get the thumbnails exactly where they said they did not want them.
    """


_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_PERCENT_VARIABLE = re.compile(r"%([A-Za-z_][A-Za-z0-9_()]*)%")


def absolute_path(value: str, base: Path) -> Path:
    """A path setting as it leaves `Settings`: absolute (RFC-034 section 6.3).

    The five rules, in order. None of them consults the disk.
    """
    if not value.strip():
        # Before the five: `THUMBNAIL_DIRECTORY=` is the path `.`, which rule
        # 4 would anchor on the folder of the file itself -- installed, the
        # thumbnails would land loose in the data directory. Nobody means
        # that, and guessing the default instead would be the silence of
        # section 5.5.
        raise ValueError(
            "the value is empty. Delete the line to use the default, or write "
            "the path after the equals sign."
        )
    if _CONTROL_CHARACTER.search(value):
        # Rule 1. No Windows path has one. It is the trace of double quotes:
        # the dotenv format reads `\f`, `\r`, `\t` and `\n` inside them, and
        # `"D:\fotos\raw\thumbs"` arrives as six fragments (section 2.4).
        raise ValueError(
            f"{ascii(value)} has a control character in it. Double quotes turn "
            "the \\f, \\n, \\r and \\t of a Windows path into one: write the "
            "path without quotes, spaces included."
        )
    variable = _PERCENT_VARIABLE.search(value)
    if variable:
        # Rule 2. The format does not expand this form, and the result would
        # be a folder called `%LOCALAPPDATA%`.
        name = variable.group(1)
        raise ValueError(
            f"{value!r} uses %{name}%, which is not expanded here. "
            f"Write ${{{name}}} instead."
        )
    path = Path(value)
    if not path.is_absolute():
        # Rule 3: the two halfway forms of Windows.
        if path.drive:
            raise ValueError(
                f"{value!r} names a drive and no root, so it is relative to "
                f"whatever that drive's current directory is. Write "
                f"{path.drive}\\... instead."
            )
        if path.root:
            raise ValueError(
                f"{value!r} starts with a slash and names no drive, so it "
                "inherits the drive of wherever it is read from. Name the drive."
            )
        # Rule 4: relative to the folder of the configuration file.
        path = base / path
    # Rule 5.
    return Path(os.path.normpath(path))


class _LayoutDefaults(PydanticBaseSettingsSource):
    """The weakest source: the defaults of the layout being loaded.

    The path defaults and the mode defaults. The defaults written on the
    fields are the running code's layout's. This is what gives any other
    layout its own.
    """

    def __init__(self, settings_cls: type[BaseSettings], where: Layout) -> None:
        super().__init__(settings_cls)
        self._where = where

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        defaults = {**PATH_DEFAULTS, **MODE_DEFAULTS}
        return {name: default(self._where) for name, default in defaults.items()}


class _Without(PydanticBaseSettingsSource):
    """Another source, minus the keys that have no effect in this mode."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        source: PydanticBaseSettingsSource,
        dropped: Sequence[str],
    ) -> None:
        super().__init__(settings_cls)
        self._source = source
        self._dropped = frozenset(dropped)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in self._source().items()
            if name not in self._dropped
        }


class Settings(BaseSettings):
    """Strongly typed application settings for SolidVision.

    `Settings()` reads the sources of the layout the running code was found
    in. A `Settings` for any other layout comes from `load_settings`.
    """

    model_config = SettingsConfigDict(
        env_file=layout.config_file,
        env_file_encoding=CONFIG_FILE_ENCODING,
        case_sensitive=False,
        # A key the class does not know is dropped here and named in the log
        # by `load_settings`. Refusing to start over it would let an optional
        # file take the app down for a typing mistake (RFC-034 section 5.4).
        extra="ignore",
    )

    _layout: ClassVar[Layout] = layout

    project_name: str = Field(default="SolidVision", description="Application name")
    project_version: str = Field(default="0.1.0", description="Application version")
    debug: bool = Field(default=False, description="Enable debug mode")

    database_source: DatabaseSource = Field(
        default=DatabaseSource.EXTERNAL,
        description="Where the database connection comes from",
    )
    """`external` or `embedded` (RFC-034 section 7.1).

    `external` is the five `DATABASE_*` fields below: the `.env` and the
    Docker container. `embedded` is the user's cluster, whose port and
    password the engine asks the adapter for at every new connection; none of
    the five fields is read then, not even as a fallback (section 7.4).

    Installed, this is always `embedded`, whatever a file says. In a checkout,
    `DATABASE_SOURCE=embedded` in the `.env` is development without Docker.
    """

    database_host: str = Field(default="localhost", description="Database host")
    database_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="Database port",
    )
    database_name: str = Field(default="solidvision", description="Database name")
    database_user: str = Field(default="solidvision", description="Database user")
    # Out of `repr`, and so out of every log line and traceback that prints
    # the settings (RFC-034 section 7.7). The embedded cluster's password is
    # not here at all.
    database_password: str = Field(
        default="solidvision",
        description="Database password",
        repr=False,
    )

    # `embedding_model` used to sit here and was removed by RFC-035. Since the
    # models load from a directory checked against a lockfile in the code, the
    # setting could only agree with that directory -- redundant -- or disagree
    # and lie. The model is part of the version of the code, like the
    # migration that fixes `vector(512)` (RFC-035 section 4.4).
    embedding_dimension: int = Field(
        default=512,
        ge=1,
        description="Embedding vector size produced by the model of the lockfile",
    )
    """A wrong value here does not swap the space in silence: the adapter
    refuses every vector, on the first image, naming this setting."""
    device: str = Field(
        default="auto",
        description="Inference device: 'auto' (CUDA when available, else CPU), "
        "'cpu', or an explicit torch device string",
    )

    default_collection_name: str = Field(
        default="default",
        description="Default collection name",
    )
    # Two window sizes, deliberately separate (RFC-024 sections 5 and 7.1).
    #
    # `batch_size` is bounded by memory: every image in a batch is decoded
    # and preprocessed before the forward pass, so raising it costs RAM in
    # proportion to the source photos. 8 is the shipped default because the
    # measured CPU speedup curve plateaus there -- batch 8 captures ~87% of
    # all the speedup available out to batch 128, and batch 64 was measurably
    # *slower* than 32, so bigger is not reliably better. It is a default,
    # not a ceiling: raise it on a machine with memory to spare.
    #
    # `metadata_prefetch_size` is bounded by nothing comparable: it controls
    # how many ids go into one SELECT of four scalar columns. Coupling the
    # two would force a bad compromise in both directions -- 8 ids per query
    # is barely better than the per-file round trips this exists to remove,
    # and 512 decoded images at once would exhaust memory on real photos.
    batch_size: int = Field(
        default=8,
        ge=1,
        description="Images encoded per model forward pass by the indexing worker",
    )
    metadata_prefetch_size: int = Field(
        default=512,
        ge=1,
        description="Discovered files whose index metadata is read back per query",
    )
    # RFC-028 section 6 reads the EXIF capture date during the scan, for
    # every discovered file, including the ones the incremental check then
    # skips -- that is how an already-indexed photo gains a date without
    # paying for inference. Section 11 names the risk: a small per-file cost
    # times 100,000 files can still be a large total. The switch exists so
    # that a measured cost can be answered by configuration rather than a
    # code change. Off means "not examined", never "no date": rows scanned
    # with it off keep `capture_source = NULL` and are picked up by the next
    # scan that has it on, or by `exif_backfill`.
    extract_capture_date: bool = Field(
        default=True,
        description="Read each discovered file's EXIF capture date during the scan",
    )
    # RFC-032 section 4.2: the GPS position, read in the same open of the
    # file as the capture date -- one header read, two facts -- so turning
    # it on costs a parse of the GPS IFD, not a second open. The same rule
    # as its twin: off means "not examined", never "no position", and rows
    # scanned with it off keep `position_source = NULL` for the next scan
    # with it on, or for `exif_backfill`.
    extract_gps: bool = Field(
        default=True,
        description="Read each discovered file's EXIF GPS position during the scan",
    )
    worker_count: int = Field(
        default=1,
        ge=1,
        description="Indexing job executors this machine is expected to run",
    )
    """Still without a reader in the code, and RFC-029 says why.

    RFC-029 section 9 expected this to gain a consumer: several devices can
    be indexed at once, one executor each. Nothing here spawns them,
    because nothing here needs to -- an executor is
    `python -m app.infrastructure.workers.job_runner`, and running two of
    them is running the command twice. The pieces that would break under
    concurrency are built for it and tested for it (the partial unique
    index of section 9, and the `SKIP LOCKED` claim of section 4.10), so
    the setting documents the expectation while the operating system
    supplies the process manager.

    A supervisor that read this and forked would be a second way to start
    the same program, and the first thing to get out of step with how it
    is actually deployed -- a native Windows process, not a container
    (RFC-029 section 6).
    """

    # RFC-029's four knobs. The defaults below are chosen against the
    # measurements in `experiments/rfc-029-indexing-jobs/`, which is
    # where the numbers behind each one live -- RFC-029 sections 6.1 and
    # 9.1 both insisted the timeout in particular come from a measured
    # distribution rather than from "a few minutes".
    job_poll_interval: float = Field(
        default=2.0,
        gt=0.0,
        description="Seconds the job executor sleeps between polls of an empty queue",
    )
    """Start-up latency for a job, against an operation lasting minutes.

    The declared cost of polling instead of pushing (RFC-029 section 6.1):
    a job created just after a poll waits up to this long. Lowering it
    trades idle queries for a shorter wait, and the measurement says how
    much of each.
    """

    job_heartbeat_interval: float = Field(
        default=10.0,
        gt=0.0,
        description="Minimum seconds between heartbeat writes during a scan",
    )
    """Rate limit for the heartbeat emitted *while walking the disk*.

    Progress written at the end of a window or a batch is not rate-limited
    -- that is already at most one write per 512 files or per 8 images
    (RFC-029 section 7.3). This bounds the one callback that fires per
    file, so a scan cannot turn into a write per file.
    """

    job_stale_timeout: float = Field(
        default=120.0,
        gt=0.0,
        description="Seconds without a heartbeat after which a job is abandoned",
    )
    """How long silence has to last before the reaper acts.

    Must exceed the longest real gap between heartbeats, which is not the
    length of a batch: a cold scan and a resume can both go a long time
    without reaching one. Too short and the reaper kills healthy jobs; too
    long and a device stays reserved after a genuine crash.
    """

    job_max_attempts: int = Field(
        default=3,
        ge=1,
        description="Times a job may be requeued by the reaper before it fails",
    )
    """The bound that stops a requeue loop from running for ever.

    The reaper puts an abandoned job back in the queue with its checkpoint
    (RFC-029 section 9.1, corrected), which is what makes the checkpoint
    have a reader at all. Without a bound, a file whose decoder takes the
    whole process down would kill every worker that resumed onto it.
    """
    # `NoDecode` opts this field out of pydantic-settings' default behaviour
    # for complex types: without it, the dotenv/env source runs the raw
    # string through `json.loads()` before validation ever sees it, and
    # ".jpg,.jpeg,.png" is not JSON -- every `.env` shipped by this project
    # would fail to load. With it, the raw string reaches
    # `parse_supported_extensions` below, which does the comma-splitting
    # that `SUPPORTED_EXTENSIONS=.jpg,.jpeg,...` in `.env.example` expects.
    supported_extensions: Annotated[tuple[str, ...], NoDecode] = Field(
        default=SUPPORTED_IMAGE_EXTENSIONS,
        description="Supported image extensions",
    )
    # `indexing_root_path` used to sit here and was removed by RFC-034, on
    # the precedent `minimum_similarity` set below: it had no reader. The
    # indexing worker requires `--root`, and jobs index by device and scopes
    # (RFC-029). Left in place, its relative default would also have been the
    # one path setting that moved with the working directory.

    # Injected into `SearchImagesUseCase` by the composition root as its
    # default page size; the use case itself never reads settings.
    #
    # `minimum_similarity` used to sit here and was removed by RFC-025.
    # It had no readers at all, and its declared `ge=0.0, le=1.0` range
    # was wrong for the scores search actually produces: cosine
    # similarity runs in [-1, 1], so the bound would have rejected
    # legitimate configuration for a filter that did not exist. A
    # similarity floor is future work, and it needs the measured
    # distribution of real scores to choose a default -- not a range
    # guessed before anything was ranked.
    top_k_results: int = Field(
        default=10,
        ge=1,
        description="Default number of search results returned per query",
    )

    min_radius_m: float = Field(
        default=300.0,
        ge=0.0,
        description="Smallest search radius, in metres, a 'near here' search accepts",
    )
    """The floor under a search circle (RFC-032 sections 2.2 and 6). Injected.

    The GPS records where the drone was, and an oblique shot shows ground
    tens to hundreds of metres from it. A circle smaller than that offset is
    a machine for wrong answers: it hides, with complete confidence and no
    error, exactly the photo the user is looking for. So this is product
    policy, not a UI detail -- and Application policy, injected into
    `SearchImagesUseCase` by the composition root, never read by it.

    300 m is measured, by a criterion stated before the number: the 95th
    percentile of the aircraft-to-subject ground offset, `altitude /
    tan(|gimbal pitch|)`, over the real DJI files available, rounded up to
    the next 50 m (`experiments/rfc-032-geolocation/measure_gps_cost.log`:
    p50 96 m, p95 274 m, max 358 m, over the 20 frames whose camera records
    its pitch -- the FC3682 writes a placeholder `0.00` and was excluded).
    A larger sample of the collection may move it; the script re-measures.
    """

    max_map_cells: int = Field(
        default=2000,
        ge=1,
        description="Most cells GET /images/map returns before coarsening its grid",
    )
    """The ceiling on one map answer (RFC-032 section 7). Injected.

    Above it the grid is coarsened a decimal place at a time and the
    response says so in `precision_applied`, rather than truncating the
    list -- coarser squares hide no photo, a cut list hides whole places.

    2000 is measured, by a criterion that bounds what the ceiling exists to
    bound -- the payload: the largest of 500/1000/2000/5000 whose worst-case
    answer stays within 256 KB, at the 54.8 bytes per cell measured on the
    real `MapResponseSchema` JSON (2000 cells -> ~107 KB; 5000 -> ~268 KB).
    `experiments/rfc-032-geolocation/planner_check.log` has the numbers,
    and the first version of the criterion, which also bounded time and
    selected nothing because the time it measured was the full scan, not
    the ceiling.
    """

    # Loading the CLIP checkpoint at API startup closes two problems at
    # once (RFC-026 sections 9.1 and 10): the ~5 s cold start the first
    # query would otherwise pay, and the check-then-act race inside the
    # adapter's lazy load, which only exists because a `def` endpoint runs
    # in a threadpool and two concurrent first requests can both find the
    # model unloaded.
    #
    # The default comes from the mode (RFC-035 section 8): on installed, off
    # in a checkout. RFC-026 chose `False` everywhere so that importing the
    # app never downloaded 600 MB; loading no longer downloads anything, but
    # the example still holds for another reason: `TestClient(app)` used as a
    # context manager runs startup events, so a `True` in a checkout would
    # make `tests/presentation/test_health.py` load and hash 924 MB, or fail
    # without `runtime\models`. Installed there is no suite, and nobody
    # writes the user's file: a `False` there would leave the API cold for
    # ever. The file can still change it in both modes.
    warm_up_models: bool = Field(
        default_factory=_mode_default("warm_up_models"),
        description="Load the embedding model at API startup "
        "instead of on the first request",
    )

    # RFC-030 section 6, guard 1. It defaults to `False` on the precedent
    # `warm_up_models` set: the safe default is the one that cannot act on
    # a machine that never asked. Here the action is opening windows on the
    # user's desktop, so an API started without this setting answers 404
    # for `/reveal` -- the route behaves as if it did not exist, rather
    # than existing and refusing.
    #
    # It is *one* of two guards, and it does not replace the other: however
    # this is set, `/reveal` refuses any caller that is not on loopback.
    # This one is the operator's intention; that one is a fact about who is
    # calling. The file path in search results is deliberately not behind
    # this setting -- showing where a photo is is the product's main job.
    allow_local_file_actions: bool = Field(
        default=False,
        description="Allow POST /api/v1/images/{id}/reveal to open the file "
        "manager on this machine (loopback callers only)",
    )

    api_port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="The port the launcher serves the API on; unset, it chooses one",
    )
    """Unset, the launcher of RFC-036 chooses the port (section 5.4).

    The port of the last start that reached `ready` if it is still free, and
    otherwise one the system gives; never a fixed 8000, which is one of the
    ports development servers use most. Set, it is a promise to somebody -- a
    script, the proxy of a frontend's development server -- and the launcher
    uses exactly this port or refuses to start, with a line saying so, rather
    than choosing another. The address is always `127.0.0.1`, and is not a
    setting: serving the network is not something this API does (RFC-030
    section 6).
    """

    # The default of each of the five path settings comes from the layout,
    # through `PATH_DEFAULTS`, because it depends on the mode. What is written
    # on the field is the default for the layout of the running code; for any
    # other layout, `load_settings` supplies that layout's. Each path is
    # absolute by the time it leaves this class, whoever wrote it and however
    # (RFC-034 section 6.3).
    thumbnail_directory: Path = Field(
        default_factory=_layout_default("thumbnail_directory"),
        description="Directory the application keeps thumbnails in",
    )
    """The thumbnail cache (RFC-030 section 7.1). Never inside a collection.

    The default is `thumbnails` under the user's data directory --
    `%LOCALAPPDATA%\\SolidVision`, or `SolidVision-dev` in a checkout, so that
    a developer's thumbnails and an installed app's never share a folder:
    they are named by image id, and an id only means something inside one
    database (RFC-034 section 2.5). Wherever it is, scans skip it
    (`FilesystemImageProvider(excluded_directories=...)`), so indexing a
    whole system disk does not index the thumbnails.
    """

    thumbnail_max_edge: int = Field(
        default=512,
        ge=16,
        description="Longest side, in pixels, of a generated thumbnail",
    )
    """512, the size RFC-030 section 7.2 used for its example.

    Changing it does not re-render existing thumbnails -- run the
    thumbnail backfill with `--force` for that. Their `ETag` does not
    change either, because it is the content hash of the *photo*: a client
    that already holds a thumbnail at the old size keeps it until the photo
    itself changes, which is a smaller picture of the right photo rather
    than a wrong one.
    """

    embedded_postgres_runtime_directory: Path = Field(
        default_factory=_layout_default("embedded_postgres_runtime_directory"),
        description="Directory holding the vendored PostgreSQL runtime",
    )
    """Where the embedded binaries are (RFC-033 section 5.2). Never the `PATH`.

    The default is `runtime\\postgres` under the layout's root. In a checkout
    that is the repository root, and `scripts/vendor_postgres.py` materializes
    the runtime there; installed, it is wherever the installer puts the code,
    with the runtime beside it.

    `EMBEDDED_` keeps both names out of the `POSTGRES_*` namespace, which the
    Docker image reads from the same `.env`.
    """

    model_runtime_directory: Path = Field(
        default_factory=_layout_default("model_runtime_directory"),
        description="Directory holding the two models, materialized from the lockfile",
    )
    """Where CLIP and the translator are loaded from (RFC-035 section 4.5).

    The default is `runtime\\models` under the layout's root, beside
    `runtime\\postgres`: `scripts/vendor_models.py` materializes it in a
    checkout, and the installer puts it beside the code. Configurable for the
    reason the PostgreSQL runtime is: several checkouts can share one tree
    instead of keeping 924 MB each. Pointing it elsewhere cannot change the
    model, because whatever tree it names is checked against the lockfile of
    the code before anything in it is loaded (section 6).
    """

    embedded_postgres_directory: Path = Field(
        default_factory=_layout_default("embedded_postgres_directory"),
        description="Per-user directory the embedded PostgreSQL cluster lives in",
    )
    """The user's cluster (RFC-033 section 6): `postgres` under the data directory.

    `LOCALAPPDATA` for the thumbnails' reason and one more: OneDrive does not
    sync it, and a sync client that locks or rewrites files inside a live
    cluster corrupts the cluster. The adapter adds the major version below
    this (`postgres\\16\\data`), so a future `17\\` fits beside it. A checkout's
    cluster and an installed app's are apart by default since RFC-034; the
    setting is for tests, for diagnosis, and for a system disk too small to
    hold the cluster.
    """

    log_level: str = Field(default="INFO", description="Logging level")
    log_directory: Path = Field(
        default_factory=_layout_default("log_directory"),
        description="Directory for log files",
    )
    """`logs` under the data directory (RFC-034 section 8.1).

    The server's own log stays where RFC-033 put it, in `postgres\\16\\data\\log`:
    two places, one per owner.
    """

    log_filename: str | None = Field(
        default=None,
        description="Log filename; unset, the name of the program that is running",
    )
    """Unset, each program writes its own file (RFC-034 section 8.2).

    `uvicorn.log`, `job_runner.log`, `pytest.log`: two programs appending to
    one rotating file lose the lines of whichever writes most (section 2.6).
    Setting it gives every process the one fixed name, as before.
    """

    @property
    def environment(self) -> Environment:
        """The mode of the layout these settings were loaded for.

        No longer a field: a mode someone writes down has a default that lies
        when nobody writes it (RFC-034 section 4.2). `/health` still reports it.
        """
        return type(self)._layout.environment

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """The precedence of RFC-034 section 5.1, decided by the mode."""
        defaults = _LayoutDefaults(settings_cls, cls._layout)
        if cls._layout.environment is Environment.INSTALLED:
            user_file = _Without(settings_cls, dotenv_settings, DATABASE_KEYS)
            return (init_settings, user_file, defaults)
        return (init_settings, env_settings, dotenv_settings, defaults)

    @field_validator(*PATH_DEFAULTS, mode="before")
    @classmethod
    def anchor_path(cls, value: object) -> object:
        """Make every path absolute here, once, instead of in each consumer."""
        if isinstance(value, str | Path):
            return absolute_path(str(value), cls._layout.config_base)
        return value

    @field_validator("database_source", mode="before")
    @classmethod
    def normalize_database_source(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def installed_is_always_embedded(self) -> Self:
        """Installed, there is no external database to point at (section 7.1)."""
        if type(self)._layout.environment is Environment.INSTALLED:
            self.database_source = DatabaseSource.EMBEDDED
        return self

    @field_validator("supported_extensions", mode="before")
    @classmethod
    def parse_supported_extensions(cls, value: object) -> object:
        """Split the comma-separated form `NoDecode` leaves untouched.

        Only strings need this: the default (already a tuple) and
        programmatic construction (e.g. in tests) pass a tuple straight
        through to `validate_supported_extensions`.
        """
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("supported_extensions")
    @classmethod
    def validate_supported_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Ensure supported extensions are normalized to lowercase."""
        return tuple(ext.lower() for ext in value)


@dataclass(frozen=True)
class ConfigurationReport:
    """Where one process's configuration came from (RFC-034 section 5.6).

    What makes the precedence checkable on a user's machine: the log says
    which file was read. `settings` cannot log it -- the logger imports this
    module -- so it is returned beside the `Settings`, and the logger writes
    it the first time it is configured. The password is not in it.
    """

    environment: Environment
    config_file: Path
    config_file_exists: bool
    data_directory: Path
    directories: tuple[tuple[str, Path], ...]
    database_source: DatabaseSource
    warnings: tuple[str, ...]
    """Keys that do not do what they seem to (section 5.4), by name."""

    def lines(self) -> tuple[str, ...]:
        found = "read" if self.config_file_exists else "absent, defaults in use"
        return (
            f"Configuration: mode {self.environment.value}",
            f"Configuration: file {self.config_file} ({found})",
            f"Configuration: data directory {self.data_directory}",
            *(f"Configuration: {name} {path}" for name, path in self.directories),
            f"Configuration: database source {self.database_source.value}",
        )


class LoadedSettings(NamedTuple):
    settings: Settings
    report: ConfigurationReport


def load_settings(where: Layout, **overrides: Any) -> LoadedSettings:
    """The settings of one layout, and the report of where they came from.

    The sources are decided here, before any `Settings` is built, because
    they depend on the mode (section 5.2). `overrides` are constructor
    arguments, the strongest source in both modes; they are for tests.
    """
    file_keys = _keys_of(where.config_file)
    try:
        loaded = _settings_class(where)(**overrides)
    except ValidationError as error:
        raise ConfigurationError(_explain(error, where, file_keys)) from None
    return LoadedSettings(loaded, _report(loaded, where, file_keys))


@cache
def _settings_class(where: Layout) -> type[Settings]:
    """`Settings`, bound to a layout. One class per layout, so that two loads
    of the same layout give instances that can be compared."""
    if where == Settings._layout:
        return Settings

    class LayoutSettings(Settings):
        model_config = SettingsConfigDict(env_file=where.config_file)

        _layout: ClassVar[Layout] = where

    return LayoutSettings


def _keys_of(config_file: Path) -> tuple[str, ...]:
    """The keys written in the file, as written. Empty when there is no file."""
    if not config_file.is_file():
        return ()
    try:
        return tuple(dotenv_values(config_file, encoding=CONFIG_FILE_ENCODING))
    except UnicodeDecodeError:
        raise ConfigurationError(
            f"{config_file}: the file is not UTF-8 text. Save it again as UTF-8 "
            "(Notepad: Save As, Encoding: UTF-8)."
        ) from None


def _explain(error: ValidationError, where: Layout, file_keys: Sequence[str]) -> str:
    """One line: where each bad value was written, its key, and what is wrong.

    The value itself is left out. It may be a password.
    """
    in_file = {key.lower() for key in file_keys}
    in_environment = {name.lower() for name in os.environ}
    problems = []
    for detail in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        name = str(detail["loc"][0]) if detail["loc"] else "settings"
        if where.environment is Environment.DEVELOPMENT and name in in_environment:
            origin = "environment variable"
        elif name in in_file:
            origin = str(where.config_file)
        else:
            origin = "constructor argument"
        message = detail["msg"].removeprefix("Value error, ")
        problems.append(f"{origin}: {name.upper()}: {message}")
    return "; ".join(problems)


def _report(
    loaded: Settings, where: Layout, file_keys: Sequence[str]
) -> ConfigurationReport:
    return ConfigurationReport(
        environment=where.environment,
        config_file=where.config_file,
        config_file_exists=where.config_file.is_file(),
        data_directory=where.data_directory,
        directories=tuple((name, getattr(loaded, name)) for name in PATH_DEFAULTS),
        database_source=loaded.database_source,
        warnings=_warnings(where, file_keys),
    )


def _warnings(where: Layout, file_keys: Sequence[str]) -> tuple[str, ...]:
    """Section 5.4: the three kinds of key that fail in silence otherwise."""
    file = where.config_file
    installed = where.environment is Environment.INSTALLED
    warnings = []
    for key in file_keys:
        if key.upper() in RETIRED_KEYS:
            warnings.append(
                f"{file}: {key} is retired and was ignored: "
                f"{RETIRED_KEYS[key.upper()]}."
            )
        elif installed and key.lower() in DATABASE_KEYS:
            warnings.append(
                f"{file}: {key} has no effect here and was ignored: an installed "
                "SolidVision always uses its own embedded database."
            )
        elif key.lower() not in Settings.model_fields:
            warnings.append(
                f"{file}: {key} is not a setting and was ignored. "
                "Check the spelling against .env.example."
            )
    # Notepad saves a new file named `solidvision.env` as `solidvision.env.txt`
    # and hides the extension, so the file the user sees is not the file that
    # is read (section 5.3; a hypothesis until section 16 tests it).
    mistaken = file.with_name(file.name + ".txt")
    if installed and not file.is_file() and mistaken.is_file():
        warnings.append(
            f"{mistaken} exists and {file.name} does not: the configuration file "
            f"was saved with a .txt extension and was not read. Rename it to "
            f"{file.name}."
        )
    return tuple(warnings)


def _load_or_exit() -> LoadedSettings:
    """The process's own settings, or one line on stderr and exit code 78.

    No traceback (section 5.5): whoever reads this is a user or the launcher
    of RFC-036, and the line already names the file and the key. 78 is what
    tells the launcher that restarting will not help (RFC-036 section 6.1).
    """
    try:
        return load_settings(layout)
    except ConfigurationError as error:
        refuse_to_start(f"SolidVision cannot start: {error}")


settings, configuration_report = _load_or_exit()
