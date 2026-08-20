"""AI infrastructure package.

`ClipEmbeddingModel` is deliberately NOT re-exported here. This package's
`__init__` runs on any `from app.infrastructure.ai...` import, including the
`FakeEmbeddingModel` imports scattered through the fast test suite, and
re-exporting the CLIP adapter would drag `torch` and `transformers` into
every one of those imports for no benefit. Import it by its full module path
instead: `from app.infrastructure.ai.clip_embedding_model import
ClipEmbeddingModel`.
"""

from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel

__all__ = ["FakeEmbeddingModel"]
