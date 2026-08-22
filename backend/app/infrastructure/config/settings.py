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
    worker_count: int = Field(default=1, ge=1, description="Worker count")
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
