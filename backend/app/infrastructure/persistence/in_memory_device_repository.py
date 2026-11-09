"""Repositório de dispositivos em memória, para desenvolvimento e ligação (RFC-027)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import replace

from app.domain.entities.device import Device
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity


class InMemoryDeviceRepository(DeviceRepository):
    """Implementação do repositório sobre um dict em memória.

    Submetida ao mesmo teste de contrato que `PostgresDeviceRepository`, que é
    a única razão de valer a pena tê-la: um dublê que aceitasse uma segunda
    linha para o mesmo volume, ou que casasse identidades de forma frouxa,
    transformaria cada teste verde acima dele em evidência sobre o dublê.
    """

    def __init__(self) -> None:
        self._devices: dict[uuid.UUID, Device] = {}

    def save(self, device: Device) -> None:
        """Insere ou atualiza o dispositivo, com o id como chave.

        Uma atribuição num dict é quase um upsert de graça, que por acaso é o
        contrato. O que ela não pode virar é um acréscimo: `DeviceId` é
        derivado da identidade do volume, então salvar o mesmo disco duas vezes
        é o caso normal -- toda execução do worker faz isso --, e uma segunda
        entrada seria uma duplicata que a restrição `UNIQUE` do banco teria
        recusado.

        `first_seen_at` é trazido da linha existente em vez de sobrescrito, como
        em `PostgresDeviceRepository`. É o único campo que responde a uma
        pergunta sobre o passado, e uma atribuição simples aqui deixaria este
        dublê silenciosamente mais esquecido que o banco que ele substitui --
        transformando "conhecido desde março" em "conhecido desde agora" a cada
        execução do worker, na implementação que a maioria dos testes de fato
        exercita.
        """
        existing = self._devices.get(device.id.value)
        if existing is not None:
            device = replace(device, first_seen_at=existing.first_seen_at)
        self._devices[device.id.value] = device

    def record_scan(
        self, device_id: DeviceId, file_count: int, scanned_at: datetime.datetime
    ) -> None:
        """Substitui os dois campos de scan do dispositivo guardado, e mantém o resto.

        A partir do dispositivo como está guardado *agora*, não de uma cópia que
        quem chama tem em mãos, como o `UPDATE` de `PostgresDeviceRepository`:
        um rótulo renomeado depois de o job começar continua renomeado.
        """
        existing = self._devices.get(device_id.value)
        if existing is None:
            return
        self._devices[device_id.value] = replace(
            existing, last_scan_at=scanned_at, last_scan_file_count=file_count
        )

    def get(self, device_id: DeviceId) -> Device | None:
        return self._devices.get(device_id.value)

    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        """Acha o dispositivo de uma identidade de volume, casando as duas metades.

        `VolumeIdentity` é uma dataclass congelada, então `==` já compara a
        string opaca *e* o tipo de plataforma. Comparar só a string fundiria
        dois volumes que duas plataformas por acaso nomeassem igual.
        """
        for device in self._devices.values():
            if device.volume_identity == identity:
                return device
        return None

    def list(self) -> list[Device]:
        return list(self._devices.values())

    def delete(self, device_id: DeviceId) -> None:
        """Esquece o dispositivo, e nada mais.

        Deliberadamente não toca em imagem nenhuma: este repositório não sabe
        de imagens, e a porta proíbe a cascata de qualquer forma, porque uma
        linha de imagem é um embedding que custou tempo real de inferência
        (RFC-027 §6.3).
        """
        self._devices.pop(device_id.value, None)
