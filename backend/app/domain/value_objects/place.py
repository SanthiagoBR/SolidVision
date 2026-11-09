"""Um endereço que o geocodificador resolveu, e o que ele entendeu (RFC-043)."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.value_objects.position import Position


@dataclass(frozen=True)
class Place:
    position: Position
    # Mostrado ao lado do ponto: há muitas "Rua das Palmeiras", e só o usuário
    # sabe dizer se a encontrada é a dele.
    label: str
