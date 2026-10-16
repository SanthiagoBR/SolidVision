"""AI infrastructure package.

**The first thing this package does is set the environment of the model
libraries** (RFC-035 section 5.2): the offline mode, and where `huggingface_hub`
and `torch` may write. Python runs a package's `__init__` before any of its
submodules, and `torch` and `transformers` are imported nowhere in
`backend/app` but here, so no code path of the app reaches `huggingface_hub`
without passing through these lines first. They come before every other
import below, on purpose.

`ClipEmbeddingModel` is deliberately NOT re-exported here. This package's
`__init__` runs on any `from app.infrastructure.ai...` import, including the
`FakeEmbeddingModel` imports scattered through the fast test suite, and
re-exporting the CLIP adapter would drag `torch` and `transformers` into
every one of those imports for no benefit. Import it by its full module path
instead: `from app.infrastructure.ai.clip_embedding_model import
ClipEmbeddingModel`.
"""

from app.infrastructure.ai.environment import prepare_model_environment
from app.infrastructure.config.layout import layout

prepare_model_environment(layout)

from app.infrastructure.ai.fake_embedding_model import (  # noqa: E402
    FakeEmbeddingModel,
)

__all__ = ["FakeEmbeddingModel"]
