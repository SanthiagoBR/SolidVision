"""What the app does, and does not do, when it starts (RFC-026 section 10).

The trap this file exists for is silent. A warm-up that runs
unconditionally looks correct, makes every search test pass, and then
downloads 600 MB of checkpoint during a plain `pytest` -- because
`TestClient(app)` used as a context manager runs startup events, and
`tests/presentation/test_health.py` uses it that way for a route that
never touches a model. The damage would appear as an unrelated test file
suddenly needing a network.

Nothing here loads a real model: the provider is patched with a recorder,
so what is measured is whether startup reaches for the model at all and
what it hands it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.presentation.dependencies as dependencies_module
from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.ai.fake_embedding_model import FakeEmbeddingModel
from app.infrastructure.ai.query_translator import MarianQueryTranslator
from app.infrastructure.config.settings import settings
from app.presentation.api import _WARM_UP_QUERY, app


class RecordingEmbeddingModel(EmbeddingModelPort):
    """A model that remembers what it was asked to encode."""

    def __init__(self) -> None:
        self.encoded: list[str] = []
        self._delegate = FakeEmbeddingModel()

    def encode_text(self, text: str) -> EmbeddingVector:
        self.encoded.append(text)
        return self._delegate.encode_text(text)

    def encode_image(self, image: Image) -> EmbeddingVector:
        return self._delegate.encode_image(image)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingEmbeddingModel]:
    """Stand in for the cached provider `_warm_up_models` resolves.

    Patched on the dependencies module rather than injected, because
    warm-up is not a request and has no `Depends` graph to override: the
    lifespan hook imports the provider and calls it.
    """
    model = RecordingEmbeddingModel()
    monkeypatch.setattr(dependencies_module, "get_embedding_model", lambda: model)
    yield model


def test_startup_loads_no_model_by_default(
    recorder: RecordingEmbeddingModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default that keeps the fast suite offline.

    Chosen the way RFC-024 chose to give `--root` no default: the safe
    default is the one that cannot download a checkpoint into a process
    that never asked for one.
    """
    monkeypatch.setattr(settings, "warm_up_models", False)

    with TestClient(app):
        pass

    assert recorder.encoded == []


def test_startup_encodes_one_query_when_warm_up_is_enabled(
    recorder: RecordingEmbeddingModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "warm_up_models", True)

    with TestClient(app):
        pass

    assert recorder.encoded == [_WARM_UP_QUERY]


def test_a_failed_warm_up_does_not_stop_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient Hugging Face outage must not become a crash loop.

    Refusing to start would take `/health` down along with search -- the
    probe an operator would use to diagnose exactly this. The lazy path
    is still there: the first query retries the load and fails visibly if
    it still cannot.
    """

    def explode() -> EmbeddingModelPort:
        raise RuntimeError("hub unreachable")

    monkeypatch.setattr(settings, "warm_up_models", True)
    monkeypatch.setattr(dependencies_module, "get_embedding_model", explode)

    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200


def test_the_warm_up_query_actually_reaches_the_translator() -> None:
    """Warming with English would leave half the cold start in place.

    RFC-025 section 12 measured two loads, not one: ~4.95 s for CLIP and
    a further ~4.20 s for Marian on the first Portuguese query. Only a
    query the detector calls Portuguese walks both. `langdetect` needs
    several words to be reliable (RFC-023 section 7.1 measured it calling
    `fazenda` Turkish), so a shortened warm-up string would quietly warm
    only CLIP while still looking correct.
    """
    assert MarianQueryTranslator._is_portuguese(_WARM_UP_QUERY)
