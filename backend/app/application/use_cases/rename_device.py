"""Dar a um disco o nome pelo qual o dono o chama (RFC-031 §7).

A única coisa editável de um `Device`, e a lista do que não é editável é a
metade interessante. Todo o resto é derivado (`id`), observado
(`filesystem_label`, `total_bytes`, `last_seen_at`) ou resultado de um scan
(`last_scan_at`, `last_scan_file_count`). Aceitar edição em qualquer um deles
deixaria um cliente mentir sobre o mundo -- e o `last_scan_file_count`, em
particular, é o denominador declarado do número de "% indexado", então um
editável seria um número inventado vestido de medição (RFC-031 §4.2).

**Renomear nunca exige o disco.** É um fato sobre a nossa tabela, não sobre o
volume, e o RFC-027 §2.3 é explícito: um disco desplugado continua sendo um
dispositivo que o sistema conhece perfeitamente. O describer, abaixo, só é
consultado para *relatar* se o disco por acaso está aqui, e responder "não" é
um resultado tão bem-sucedido quanto responder "sim".
"""

from __future__ import annotations

import dataclasses

from app.application.use_cases.describe_devices import (
    DescribeDevicesUseCase,
    DeviceSummary,
)
from app.domain.exceptions import DeviceNotFoundError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId


class RenameDeviceUseCase:
    """Muda o rótulo de um dispositivo visto pelo usuário, e nada mais nele."""

    def __init__(
        self,
        device_repository: DeviceRepository,
        describer: DescribeDevicesUseCase,
    ) -> None:
        self._devices = device_repository
        self._describer = describer

    def execute(self, device_id: DeviceId, label: str) -> DeviceSummary:
        """Renomeia o dispositivo; um id desconhecido levanta `DeviceNotFoundError`.

        Lê a linha e escreve uma cópia dela com um campo trocado, em vez de
        emitir um update dirigido. `Device` é congelado, então
        `dataclasses.replace()` *é* a edição; e, como parte da linha guardada,
        não há como isto carregar um valor velho de algo que não pretendia
        tocar -- que é o modo de falha que um `Device(...)` montado à mão aqui
        teria.

        `save()` é um upsert pela chave do id derivado (RFC-027), então isto
        atualiza a única linha, em vez de criar uma segunda.

        Devolve um resumo completo, e não a entidade crua, para que a rota
        responda no mesmo formato que `GET /devices` usa, e um cliente possa
        pôr a resposta direto na linha que acabou de renomear. A alternativa
        -- publicar um corpo de dispositivo com `connected` e `indexed_images`
        fixos no código porque esta rota não olhou -- seria uma resposta
        *errada*, e não só parcial, e um cliente que confiasse nela apagaria
        um disco conectado.
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        renamed = dataclasses.replace(device, label=label)
        self._devices.save(renamed)
        return self._describer.execute_one(renamed)
