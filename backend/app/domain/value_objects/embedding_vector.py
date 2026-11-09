"""Value object imutável do vetor de embedding para a camada de domínio."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.exceptions import InvalidEmbeddingVectorError


@dataclass(frozen=True)
class EmbeddingVector:
    """Value object imutável que representa um vetor de embedding semântico."""

    values: tuple[float, ...]

    def __init__(self, values: Iterable[float]) -> None:
        normalized_values = self._normalize(values)
        object.__setattr__(self, "values", normalized_values)

    @staticmethod
    def _normalize(values: Iterable[float]) -> tuple[float, ...]:
        normalized_values = tuple(float(value) for value in values)
        if not normalized_values:
            raise InvalidEmbeddingVectorError("Embedding vector cannot be empty")
        return normalized_values


# Um value object SearchQuery dedicado foi considerado pelo RFC-025 e adiado
# de propósito. A busca agora valida a consulta -- recusando texto em branco
# -- mas esse é todo o comportamento, e um value object cujo único trabalho é
# levar uma string já verificada não compra nada que o caso de uso já não
# tenha: todo ponto de construção ficaria a uma chamada do único lugar que
# valida. Ele passa a valer um tipo próprio quando houver comportamento de
# domínio para pôr nele (uma normalização com que o repositório precise
# concordar, filtros estruturados, uma consulta que o modelo reescreve), e
# nesse momento a validação vem para cá junto com ele.
