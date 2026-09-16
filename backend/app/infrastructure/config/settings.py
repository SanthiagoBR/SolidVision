"""Application configuration settings loaded from environment variables."""

from __future__ import annotations

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


settings = Settings()
