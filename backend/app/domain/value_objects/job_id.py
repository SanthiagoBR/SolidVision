"""Identificador de job de indexação para a camada de domínio (RFC-029)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from uuid import UUID

from app.domain.exceptions import InvalidJobIdentifierError


@dataclass(frozen=True)
class JobId:
    """Identificador fortemente tipado de um job de indexação.

    Modelado em `DeviceId` e `ImageId`, para que um `uuid.UUID` solto não
    possa ser passado onde se queria um job. Esse é todo o trabalho dele:
    agora existem três tabelas com cara de id, e todo método de repositório
    abaixo recebe uma delas.

    **Criado com `uuid4`, e não derivado.** `ImageId` e `DeviceId` são
    `uuid5` sobre algo durável, porque o mesmo arquivo e o mesmo disco
    precisam cair na mesma linha toda vez que são vistos. Um job é o tipo
    oposto de coisa: é um *evento*, e pedir para indexar a mesma pasta duas
    vezes são dois jobs, e não uma linha escrita duas vezes (RFC-029 §5.1).
    """

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        object.__setattr__(self, "value", self._normalize(value))

    @staticmethod
    def new() -> JobId:
        """Cria um identificador para um job que está sendo criado agora."""
        return JobId(uuid.uuid4())

    @staticmethod
    def _normalize(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value

        if isinstance(value, str):
            if not value.strip():
                raise InvalidJobIdentifierError("Job identifier cannot be empty")
            try:
                return UUID(value)
            except ValueError as exc:
                raise InvalidJobIdentifierError(
                    "Job identifier must be a valid UUID"
                ) from exc

        raise InvalidJobIdentifierError("Job identifier must be a UUID or string")

    def __str__(self) -> str:
        return str(self.value)
