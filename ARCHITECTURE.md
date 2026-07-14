# SolidVision Architecture

**Version:** 1.0  
**Status:** Draft  
**Last Updated:** July 2026

---

# 1. Project Overview

SolidVision is an AI-powered semantic image retrieval system designed to help professional photographers efficiently manage large local image collections. The project was conceived to solve a common problem faced by aerial photographers: locating specific photographs among hundreds of thousands of images stored over many years.

Unlike traditional file explorers that rely on folder structures or manually assigned filenames, SolidVision enables users to search their collections using natural language. A user can describe the desired image (e.g., *"rural property with a lake"*), and the system retrieves the most semantically similar photographs.

The system operates entirely on local storage. Images are never uploaded to external servers, preserving user privacy while minimizing storage costs. Instead, SolidVision indexes local folders, extracts semantic embeddings using a multimodal AI model, and stores only metadata and vector representations in a PostgreSQL database.

The architecture has been designed with long-term maintainability in mind, allowing future replacement of AI models, databases, or user interfaces with minimal impact on the rest of the system.

---

# 2. Architectural Goals

The architecture of SolidVision is designed around the following goals:

## Maintainability

Business rules must remain independent of frameworks, databases and AI libraries.

## Scalability

The system should support collections containing at least **100,000 indexed images** while maintaining fast semantic search.

## Extensibility

The embedding model should be replaceable without affecting business logic.

Examples:

- SigLIP
- CLIP
- OpenCLIP
- Future fine-tuned aerial photography models

## Testability

Core business logic must be fully testable without requiring:

- PostgreSQL
- PyTorch
- HuggingFace
- FastAPI

## Separation of Concerns

Each layer has a single responsibility.

Presentation handles HTTP.

Application orchestrates use cases.

Domain contains business rules.

Infrastructure communicates with external systems.

## Local-First Design

SolidVision never duplicates image files.

The filesystem is considered the single source of truth.

Only metadata, thumbnails and embeddings are stored in the database.

---

# 3. Core Principles

SolidVision follows the following architectural principles.

- Clean Architecture
- SOLID
- Repository Pattern
- Dependency Injection
- Separation of Concerns
- High Cohesion
- Low Coupling
- Explicit Dependencies
- Composition over Inheritance
- Domain Driven Design (where appropriate)

Business logic must remain framework-independent.

No framework-specific code should exist inside the Domain layer.

---

# 4. Technology Stack

## Backend

- Python 3.12
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL
- pgvector
- Pillow
- Pydantic
- Pydantic Settings

## Artificial Intelligence

Default model

- SigLIP

Future supported adapters

- CLIP
- OpenCLIP
- Florence
- Custom fine-tuned models

## Frontend

- React
- TypeScript
- Vite
- TailwindCSS

## Infrastructure

- Docker
- Docker Compose

## Testing

- Pytest
- pytest-mock
- React Testing Library

---

## Architecture Decision Record — ADR-001

### Decision

Use **SigLIP** as the default embedding model.

### Reason

SigLIP provides stronger semantic alignment between images and natural language than the original CLIP architecture, especially for zero-shot retrieval tasks.

Its integration with the HuggingFace ecosystem also simplifies deployment and future experimentation.

### Tradeoffs

- Slightly slower inference than CLIP
- Higher memory consumption during embedding generation
- Requires complete reindexing if replaced by another embedding model

---

# 5. High Level Architecture

SolidVision follows the principles of Clean Architecture.

```text
                ┌──────────────────────┐
                │     Presentation     │
                │      (FastAPI)       │
                └──────────┬───────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │     Application      │
                │     (Use Cases)      │
                └──────────┬───────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │        Domain        │
                │ Business Rules Only  │
                └──────────────────────┘
                           ▲
                           │
                ┌──────────┴───────────┐
                │    Infrastructure    │
                │ AI • Database • FS   │
                └──────────────────────┘
```

Dependencies always point toward the Domain.

Infrastructure implements interfaces defined by the Domain and Application layers.

The Domain never imports external libraries or frameworks.

---

# 6. Layer Responsibilities

## Presentation

Responsible for:

- HTTP endpoints
- Request validation
- Response serialization
- Authentication (future)
- Dependency injection

Presentation must never contain business rules.

---

## Application

Contains the system use cases.

Examples:

- Create Collection
- Delete Collection
- Index Collection
- Search Images
- Update Collection

The Application layer orchestrates workflows but does not implement infrastructure details.

---

## Domain

The Domain contains only business concepts.

It must remain pure Python.

Contains:

- Entities
- Value Objects
- Repository interfaces
- Domain services

The Domain knows nothing about:

- FastAPI
- SQLAlchemy
- PostgreSQL
- Torch
- Transformers
- Pillow

---

## Infrastructure

Infrastructure contains concrete implementations.

Examples:

- PostgreSQL repositories
- SigLIP adapter
- File scanner
- Thumbnail generator
- Metadata extractor
- Worker implementation

Infrastructure implements interfaces defined by higher layers.

---

# 7. Dependency Rules

The dependency direction follows the Dependency Inversion Principle.

Allowed relationships:

```text
Presentation
        │
        ▼
Application
        │
        ▼
Domain
```

Infrastructure does **not** own business logic.

Instead, it provides implementations for interfaces defined by the Application and Domain layers.

Example:

```text
Application
        │
        ▼
EmbeddingModelPort
        ▲
        │ implements
Infrastructure
        │
        ▼
SigLIPAdapter
```

The Domain layer must never depend on:

- FastAPI
- SQLAlchemy
- PostgreSQL
- Torch
- Transformers
- Pillow

The Application layer must never directly instantiate infrastructure components.

Dependency Injection should always provide concrete implementations.

---

# 8. Folder Structure

```text
solidvision/

├── backend/
│   └── app/
│       ├── presentation/
│       ├── application/
│       ├── domain/
│       └── infrastructure/
│
├── frontend/
│
├── docker/
│
├── docs/
│
├── tests/
│
├── README.md
├── ARCHITECTURE.md
└── docker-compose.yml
```

Each layer should remain independent and have a clearly defined responsibility.

---

# 9. AI Abstraction

Artificial Intelligence is treated as an infrastructure concern.

The business rules never communicate directly with SigLIP or any other model.

Instead, all interaction occurs through Ports.

```text
Application

↓

EmbeddingModelPort

↑

SigLIPAdapter

CLIPAdapter

OpenCLIPAdapter

FutureAdapter
```

Replacing the embedding model should require changing only the Infrastructure layer.

No Application or Domain code should be modified when introducing a new AI model.

---

# 10. EmbeddingModelPort

The embedding model interface defines the contract between the Application layer and AI implementations.

Both image and text embeddings must be generated by the same model family to ensure that vectors occupy the same semantic space.

Example interface:

```python
class EmbeddingModelPort(Protocol):

    def encode_image(
        self,
        image: Image
    ) -> EmbeddingVector:
        ...

    def encode_text(
        self,
        text: str
    ) -> EmbeddingVector:
        ...
```

Any future embedding model must implement this interface.

---

# 11. Versioning Strategy

Embedding vectors generated by different models are **not compatible**.

Therefore, every embedding stored in the database must include version metadata.

Required metadata:

- model_name
- model_version
- embedding_dimension
- created_at

Example:

```text
Image

↓

Embedding

↓

Model: siglip-so400m-patch14

↓

Dimension: 1152
```

Collections should only contain embeddings generated by a single model version.

When the embedding model changes, the collection must be reindexed.

Future versions may support multiple collections using different embedding models simultaneously.

---

# 12. Worker Architecture

Image indexing is executed by a dedicated worker rather than directly inside FastAPI endpoints.

The worker is responsible for long-running tasks and can be executed through a command-line interface.

Responsibilities:

- Scan folders
- Detect new images
- Extract metadata
- Generate thumbnails
- Generate embeddings
- Persist data

Future versions may replace the CLI worker with Celery or another distributed task queue without affecting business logic.

---

# 13. Indexing Pipeline

The indexing process follows a deterministic pipeline.

```text
Filesystem

↓

Folder Scanner

↓

Metadata Extraction

↓

Thumbnail Generation

↓

Image Preprocessing

↓

SigLIP Image Encoder

↓

Embedding Vector

↓

PostgreSQL + pgvector
```

Each image is processed only once per model version.

Future versions may support incremental indexing based on file timestamps or content hashes.

---

# 14. Search Pipeline

The semantic search pipeline mirrors the indexing pipeline.

```text
User Query

↓

SigLIP Text Encoder

↓

Text Embedding

↓

Vector Search (pgvector)

↓

Cosine Similarity

↓

Ranking

↓

Top-K Results

↓

Frontend
```

Because both images and text are embedded into the same semantic vector space, users can search collections using natural language without manually tagging images.

The search process never reloads or reprocesses original image files.

# 15. Database Design

SolidVision stores only metadata and semantic representations of images.

The original image files always remain in the local filesystem, which is considered the single source of truth.

The database acts exclusively as an index.

---

## Database Responsibilities

The database is responsible for:

- Managing image collections
- Storing image metadata
- Storing semantic embeddings
- Executing vector similarity searches
- Maintaining search history
- Tracking indexing operations
- Supporting incremental synchronization

---

## Main Tables

### Collections

Represents an indexed folder.

| Field | Description |
|--------|-------------|
| id | Primary Key |
| name | Collection name |
| root_path | Root directory |
| model_name | Embedding model used |
| model_version | Model version |
| created_at | Creation timestamp |
| updated_at | Last update |
| last_indexed_at | Last indexing execution |

---

### Images

Represents a single indexed image.

| Field | Description |
|--------|-------------|
| id | Primary Key |
| collection_id | Foreign Key |
| filename | Original filename |
| filepath | Absolute path |
| width | Image width |
| height | Image height |
| file_size | Bytes |
| last_modified | Filesystem timestamp |
| content_hash | SHA256 hash |
| thumbnail_path | Thumbnail location |

### Thumbnail Serving

Thumbnails are generated once during indexing and stored on disk (see `thumbnail_path` in the `Images` table). They are served to the frontend as static files through a dedicated FastAPI endpoint (`StaticFiles` or an equivalent controlled route), never embedded as base64 in API responses. This keeps JSON payloads small for large result sets and allows the browser to cache thumbnails independently.

---

### Embeddings

Stores semantic vectors.

| Field | Description |
|--------|-------------|
| id | Primary Key |
| image_id | Foreign Key |
| vector | pgvector |
| model_name | SigLIP |
| model_version | Version |
| embedding_dimension | Vector size |
| created_at | Timestamp |

Separating embeddings from images allows future support for multiple embedding models per image.

---

### SearchHistory

Stores search analytics.

| Field | Description |
|--------|-------------|
| id | Primary Key |
| query | Original search |
| execution_time_ms | Search latency |
| result_count | Returned images |
| searched_at | Timestamp |

Search history exists exclusively for analytics and user convenience.

It must never influence ranking.

---

### IndexingJobs

Tracks indexing executions.

| Field | Description |
|--------|-------------|
| id | Primary Key |
| collection_id | FK |
| started_at | Timestamp |
| finished_at | Timestamp |
| status | Running / Completed / Failed |
| processed_images | Counter |
| failed_images | Counter |
| last_processed_path | Absolute path of the last successfully processed image |

`last_processed_path` acts as a checkpoint. If the worker is interrupted, resuming a job means continuing the scan from this reference instead of restarting the entire collection, avoiding redundant reprocessing of already-indexed images.

This enables recovery after interruptions.

---

# 16. Vector Search Strategy

Semantic search uses PostgreSQL with pgvector.

The project adopts Approximate Nearest Neighbor (ANN) search.

## Distance Metric

Cosine Similarity.

Reason:

SigLIP embeddings are optimized for cosine distance.

---

## Index Type

HNSW (Hierarchical Navigable Small Worlds)

Reasons:

- Excellent recall
- Very low latency
- Incremental insertions
- Native pgvector support

IVFFlat may be evaluated in future benchmarks but is not the default.

---

## Search Flow

```
User Query

↓

Text Encoder

↓

Embedding

↓

HNSW Index

↓

Top K Candidates

↓

Cosine Similarity Ranking

↓

Results
```

---

## Incremental Indexing

Collections should support incremental updates.

Checking file changes must follow a cost-ascending order, since content hashing requires reading the full file from disk while timestamp and size checks only read filesystem metadata.

For every discovered file:

1. File exists in the database?

2. `last_modified` or `file_size` changed compared to the stored record?

3. If step 2 indicates a possible change, compute `content_hash` (SHA256) to confirm whether the content actually changed.

If the file is unchanged after step 2 (or confirmed unchanged after step 3):

Skip processing.

Otherwise:

Generate new embedding.

This ordering keeps incremental indexing inexpensive for the common case (most files unchanged) and reserves hashing for files that are actually candidates for reprocessing. This dramatically reduces indexing time for large collections.

---

# 17. Configuration

Configuration must never be hardcoded.

The project uses **pydantic-settings**.

Configuration belongs exclusively to Infrastructure.

Example:

```python
DATABASE_URL

MODEL_NAME

MODEL_VERSION

COLLECTION_ROOT

THUMBNAIL_SIZE

EMBEDDING_BATCH_SIZE

LOG_LEVEL
```

Settings are loaded from:

```
.env

↓

Settings()

↓

Dependency Injection
```

No environment variable should be accessed directly outside the configuration module.

---

# 18. Error Handling

Errors should be explicit.

The project must never expose internal exceptions.

Domain-specific exceptions include:

```
CollectionNotFound

ImageNotFound

FolderNotFound

EmbeddingGenerationError

UnsupportedImageFormat

DatabaseConnectionError

InvalidQueryError
```

Presentation converts exceptions into HTTP responses.

Application never returns HTTP codes.

Infrastructure never decides business behavior.

---

# 19. Logging

Every important operation must be logged.

Examples:

```
Collection created

Collection indexed

Image processed

Embedding generated

Embedding skipped

Search executed

Worker started

Worker stopped

Database connection

Unexpected exception
```

Structured logging is preferred.

Log levels:

- DEBUG
- INFO
- WARNING
- ERROR
- CRITICAL

Long-running indexing operations should periodically report progress.

---

# 20. Testing Strategy

Testing is considered a first-class architectural concern.

---

## Unit Tests

Focus:

Business rules.

Should not require:

- PostgreSQL
- FastAPI
- SigLIP
- Torch

---

## Integration Tests

Focus:

- PostgreSQL
- pgvector
- API endpoints

---

## End-to-End Tests

Validate:

Filesystem

↓

Worker

↓

Database

↓

Search

↓

Frontend

---

## Test Doubles

The AI layer must be replaceable during tests.

Example:

```
FakeEmbeddingModel
```

Implements

```
EmbeddingModelPort
```

Returns deterministic vectors.

Example:

```
"house"

↓

[1,2,3]
```

No AI model should be loaded during unit tests.

This guarantees:

- fast execution
- deterministic behavior
- isolated business validation

---

# 21. Architecture Decision Records (ADR)

## ADR-002

### Decision

Use PostgreSQL instead of a dedicated vector database.

### Reason

Simpler deployment.

Single database.

Enough performance for medium-sized collections.

### Tradeoffs

Lower scalability than Milvus or Qdrant.

---

## ADR-003

### Decision

Images remain in the local filesystem.

### Reason

Avoid duplication.

Reduce storage.

Respect user privacy.

### Tradeoffs

Filesystem changes must be monitored.

---

## ADR-004

### Decision

Separate indexing from HTTP requests.

### Reason

Indexing is long-running.

Can fail independently.

Can resume later.

Improves API responsiveness.

---

## ADR-005

### Decision

Store embedding metadata.

### Reason

Different embedding models generate incompatible vector spaces.

Each embedding stores:

- model name
- version
- dimension

Collections are internally consistent.

---

## ADR-006

### Decision

Use Repository Pattern.

### Reason

Business logic becomes database-independent.

Future migrations become easier.

## ADR-007

### Decision

Each embedding model uses a dedicated, fixed-dimension vector column. The system does not support storing embeddings of different dimensions in a single `vector` column.

### Reason

pgvector requires a fixed dimension per column (e.g. `vector(1152)`). SigLIP, CLIP and future models produce embeddings of different sizes, so a single generic column cannot hold them without padding or truncation, both of which distort cosine similarity.

### Consequence

Supporting multiple embedding models simultaneously (see Section 23, Future Roadmap) requires either:

- One `Embeddings` table per model/dimension, or
- A `collection_id`-scoped constraint ensuring all embeddings in a collection share the same `model_name`, `model_version` and `embedding_dimension`.

The current implementation adopts the second approach, already enforced conceptually in Section 11 (Versioning Strategy) and in the `Collections` table (Section 15), which stores `model_name` and `model_version` at the collection level.

### Tradeoffs

Cross-model search (comparing a SigLIP embedding to a CLIP embedding) is not possible. Migrating a collection to a new model requires full reindexing, not incremental updates.



---

# 22. Performance Targets

The following goals define the minimum acceptable performance.

| Metric | Target |
|----------|----------|
| Search latency | < 1 second |
| Index throughput | > 1 image/sec on CPU |
| Startup time | < 5 seconds |
| API response | < 100 ms (non-AI endpoints) |
| Supported collection | 100,000+ images |

Performance should be continuously measured.

Benchmark scripts belong inside:

```
scripts/

benchmark.py
```

---

# 23. Future Roadmap

Planned improvements include:

- Similar image search
- OCR indexing
- Face recognition
- Duplicate detection
- Automatic folder monitoring
- Incremental synchronization
- Batch embedding generation
- GPU acceleration
- Fine-tuned aerial model
- Desktop application (Electron or Tauri)
- Mobile companion application
- Multi-user support
- Cloud synchronization
- Distributed workers
- Hybrid metadata + semantic search

---

# 24. Guidelines for AI Assistants

This project is expected to be developed with assistance from AI coding tools such as GitHub Copilot, Claude Code, Cursor and ChatGPT.

When generating code:

## Always

- Respect Clean Architecture
- Respect dependency direction
- Follow SOLID principles
- Prefer composition over inheritance
- Use Dependency Injection
- Reuse existing services
- Add type hints
- Write docstrings for public APIs
- Keep methods small and cohesive
- Create or update tests when adding functionality
- Explain architectural changes before implementing them

## Never

- Put business rules inside FastAPI routers
- Access PostgreSQL directly from Presentation
- Couple AI models to Domain or Application
- Bypass Ports with concrete implementations
- Introduce circular dependencies
- Rewrite unrelated modules
- Create "God Classes"
- Duplicate business logic

Before creating new components, check whether an equivalent abstraction already exists.

Architectural consistency is preferred over short-term convenience.

---

# 25. Development Philosophy

SolidVision is intended to be more than an academic prototype.

The project should demonstrate professional software engineering practices while remaining simple enough for a single developer to maintain.

Every feature should satisfy three questions:

1. Is it aligned with the project's architecture?

2. Can it be tested independently?

3. Can it be replaced without affecting the rest of the system?

When trade-offs arise, prioritize:

1. Correctness
2. Maintainability
3. Simplicity
4. Performance

Performance optimizations should only be introduced after measurement.

The architecture is intentionally modular to allow future evolution.

The ultimate objective is to create a semantic image retrieval platform that can evolve from a university project into a production-ready application without requiring architectural redesign.

---

# Architecture Summary

SolidVision follows a layered architecture where:

- Business rules remain independent.
- Infrastructure provides replaceable implementations.
- AI is treated as a pluggable component.
- The filesystem is the source of truth.
- PostgreSQL indexes semantic information.
- Long-running indexing is delegated to workers.
- Search operates through vector similarity.
- Every major architectural decision is documented and justified.

This document serves as the single source of truth for all architectural decisions throughout the project's lifecycle.