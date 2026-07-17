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
        default="google/siglip-base-patch16-224",
        description="Embedding model identifier",
    )
    embedding_dimension: int = Field(
        default=1152,
        ge=1,
        description="Embedding vector size",
    )
    device: str = Field(default="cpu", description="Inference device")

    default_collection_name: str = Field(
        default="default",
        description="Default collection name",
    )
    batch_size: int = Field(default=16, ge=1, description="Processing batch size")
    worker_count: int = Field(default=1, ge=1, description="Worker count")
    supported_extensions: tuple[str, ...] = Field(
        default=SUPPORTED_IMAGE_EXTENSIONS,
        description="Supported image extensions",
    )

    top_k_results: int = Field(
        default=10,
        ge=1,
        description="Maximum number of results to return",
    )
    minimum_similarity: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum similarity threshold",
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
