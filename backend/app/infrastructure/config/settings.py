"""Application configuration settings loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import PROJECT_ENCODING, SUPPORTED_IMAGE_EXTENSIONS


class Settings(BaseSettings):
    """Strongly typed application settings for SolidVision."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[4] / ".env",
        env_file_encoding=PROJECT_ENCODING,
        case_sensitive=False,
        extra="ignore",
    )

    project_name: str = Field(default="SolidVision", description="Application name")
    project_version: str = Field(default="0.1.0", description="Application version")
    environment: str = Field(default="development", description="Runtime environment")
    debug: bool = Field(default=False, description="Enable debug mode")

    database_host: str = Field(default="localhost", description="Database host")
    database_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        description="Database port",
    )
    database_name: str = Field(default="solidvision", description="Database name")
    database_user: str = Field(default="solidvision", description="Database user")
    database_password: str = Field(
        default="solidvision",
        description="Database password",
    )

    embedding_model: str = Field(
        default="laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
        description="Embedding model identifier",
    )
    embedding_dimension: int = Field(
        default=512,
        ge=1,
        description="Embedding vector size produced by `embedding_model`",
    )
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
    # scan that has it on, or by `capture_date_backfill`.
    extract_capture_date: bool = Field(
        default=True,
        description="Read each discovered file's EXIF capture date during the scan",
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
    supported_extensions: tuple[str, ...] = Field(
        default=SUPPORTED_IMAGE_EXTENSIONS,
        description="Supported image extensions",
    )
    indexing_root_path: Path = Field(
        default=Path("data/images"),
        description="Root directory scanned by the indexing worker",
    )

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

    # Loading the CLIP checkpoint at API startup closes two problems at
    # once (RFC-026 sections 9.1 and 10): the ~5 s cold start the first
    # query would otherwise pay, and the check-then-act race inside the
    # adapter's lazy load, which only exists because a `def` endpoint runs
    # in a threadpool and two concurrent first requests can both find the
    # model unloaded.
    #
    # It defaults to `False` for the same reason RFC-024 gave `--root` no
    # default: the safe default is the one that cannot download 600 MB
    # into a process that never asked for it. `TestClient(app)` used as a
    # context manager runs startup events, so a default of `True` would
    # make a plain `pytest` reach Hugging Face through
    # `tests/presentation/test_health.py` -- a test file that has nothing
    # to do with search. Set `WARM_UP_MODELS=true` when running the API
    # for real.
    warm_up_models: bool = Field(
        default=False,
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

    thumbnail_directory: Path = Field(
        default_factory=lambda: _default_thumbnail_directory(),
        description="Directory the application keeps thumbnails in",
    )
    """The thumbnail cache (RFC-030 section 7.1). Never inside a collection.

    The default is the per-user application data directory --
    `%LOCALAPPDATA%/SolidVision/thumbnails` on Windows -- rather than a
    path relative to the working directory like `log_directory`. A relative
    default sits beside `indexing_root_path`'s `data/images`, and would be
    inside the collection the moment someone indexed `data/`. Wherever it
    is, scans skip it (`FilesystemImageProvider(excluded_directories=...)`),
    so indexing a whole system disk does not index the thumbnails.
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

    log_level: str = Field(default="INFO", description="Logging level")
    log_directory: str = Field(default="logs", description="Directory for log files")
    log_filename: str = Field(default="application.log", description="Log filename")

    @property
    def database_url(self) -> str:
        """Build the SQLAlchemy-style database connection URL."""
        return (
            f"postgresql+psycopg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        """Normalize the environment name."""
        return value.strip().lower()

    @field_validator("supported_extensions")
    @classmethod
    def validate_supported_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Ensure supported extensions are normalized to lowercase."""
        return tuple(ext.lower() for ext in value)


def _default_thumbnail_directory() -> Path:
    """The per-user application data directory, which no scan should be pointed at.

    `LOCALAPPDATA` rather than `APPDATA`: thumbnails are a cache that can be
    rebuilt from the photos, and Windows roams `APPDATA` between machines
    in a domain -- copying a hundred thousand JPEGs at every sign-in. The
    fallback for other platforms is the XDG cache location, for a process
    that will not get far there anyway (the volume adapter is Windows-only).
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SolidVision" / "thumbnails"
    return Path.home() / ".cache" / "solidvision" / "thumbnails"


settings = Settings()
