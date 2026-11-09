"""Resolução central do dispositivo de inferência dos adaptadores de IA."""

from __future__ import annotations

import torch

from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

AUTO_DEVICE = "auto"


def resolve_device(configured: str | None = None) -> torch.device:
    """Resolve o dispositivo torch que todo adaptador de IA deste pacote deve usar.

    Resolvido em um único lugar, para que a escolha do dispositivo nunca se
    espalhe pelos adaptadores (RFC-023 §9). `configured` tem como default
    `settings.device`, cujo próprio default é `auto`:

    - `auto`  -- CUDA quando o torch diz que está disponível, CPU no
      contrário, para que uma máquina de desenvolvimento só com CPU continue
      plenamente usável;
    - `cuda`  -- atendido quando disponível, e rebaixado para CPU com um
      aviso quando não está. CUDA aqui é uma otimização, nunca um requisito,
      então um pedido impossível de atender não pode derrubar a aplicação;
    - qualquer outro valor vai para `torch.device` como está.

    Backends de GPU além de CUDA (ROCm, MPS, XPU) estão fora do escopo do
    RFC-023 e não são detectados nem tratados como caso especial.
    """
    raw = configured if configured is not None else settings.device
    requested = raw.strip().lower()

    if requested == AUTO_DEVICE:
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if requested.startswith("cuda") and not torch.cuda.is_available():
        logger.warning(
            "Device %r was requested but torch reports no CUDA device; using CPU",
            requested,
        )
        return torch.device("cpu")

    return torch.device(requested)
