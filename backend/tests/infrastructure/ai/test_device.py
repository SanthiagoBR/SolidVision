"""Tests for central inference-device resolution (RFC-023 section 9)."""

from __future__ import annotations

import pytest
import torch

from app.infrastructure.ai.device import resolve_device
from app.infrastructure.config.settings import Settings, settings


@pytest.fixture()
def no_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)


@pytest.fixture()
def with_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)


def test_auto_selects_cpu_when_cuda_is_unavailable(no_cuda: None) -> None:
    assert resolve_device("auto") == torch.device("cpu")


def test_auto_selects_cuda_when_available(with_cuda: None) -> None:
    assert resolve_device("auto") == torch.device("cuda")


def test_explicit_cpu_is_honoured_even_when_cuda_is_available(
    with_cuda: None,
) -> None:
    assert resolve_device("cpu") == torch.device("cpu")


def test_explicit_cuda_degrades_to_cpu_rather_than_raising(no_cuda: None) -> None:
    """CUDA is an optimization, never a requirement (RFC-023 section 9).

    A machine configured for CUDA that loses it -- a CPU-only wheel, a
    container without the runtime -- must still start and serve, slowly,
    rather than failing to construct the adapter at all.
    """
    assert resolve_device("cuda") == torch.device("cpu")


def test_explicit_cuda_is_used_when_available(with_cuda: None) -> None:
    assert resolve_device("cuda").type == "cuda"


def test_resolution_is_case_and_whitespace_insensitive(no_cuda: None) -> None:
    assert resolve_device("  AUTO  ") == torch.device("cpu")


def test_falls_back_to_the_configured_setting(
    no_cuda: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing nothing must read `settings.device`, not a hardcoded default."""
    monkeypatch.setattr(settings, "device", "cpu")

    assert resolve_device() == torch.device("cpu")


def test_shipped_settings_default_is_auto() -> None:
    """The default must adapt to the host rather than pinning it to CPU.

    Asserted against the field default rather than the loaded singleton so
    a developer's own `DEVICE=` in `.env` does not fail the suite.
    """
    assert Settings.model_fields["device"].default == "auto"
