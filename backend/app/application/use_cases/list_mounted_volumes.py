"""O que está plugado, e o que disso já conhecemos (RFC-031 §5).

A pergunta que vem *antes* de registrar um disco, e a razão de
`POST /api/v1/devices` nunca precisar aceitar um caminho: o servidor enumera
os volumes e cunha um identificador opaco para cada um, e o cliente devolve
um deles. O cliente nunca compõe um local, e um identificador que ele
inventou não resolve para nada (RFC-031 §5.1).

O `device_id` de cada linha é o que deixa a tela de "adicionar um
dispositivo" mostrar os discos que já conhece como já adicionados, em vez de
deixar o usuário registrar um duas vezes e descobrir depois que nada
aconteceu.

**Esta rota é, de propósito, a cara.** O RFC-031 §5 a chama de *"a direct
projection of `mounted_volumes()`"*; ela não é. `mounted_volumes()` devolve
identidades e pontos de montagem e não conhece nem o rótulo do sistema de
arquivos nem a capacidade, que estão os dois no corpo da resposta -- então
isto passa por `VolumeCatalog.list_mounted()`, que paga uma chamada de
identificação por volume. A §4.1 do build prompt registra por que esse custo
ganhou um método próprio, em vez de ser dobrado no barato.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.repositories.device_repository import DeviceRepository
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.mounted_volume import MountedVolume


@dataclass(frozen=True)
class CatalogedVolume:
    """Um volume conectado, e o dispositivo que ele já é -- ou `None`."""

    volume: MountedVolume

    device_id: DeviceId | None
    """O dispositivo registrado para este volume, ou `None` se ele é novo aqui.

    Não é derivado pelo hash da identidade, embora a derivação seja
    determinística e desse a mesma resposta. `compute_device_id()` diz como
    um dispositivo *seria* chamado; este campo diz se uma linha existe, que é
    a pergunta que a tela está fazendo.
    """

    @property
    def is_registered(self) -> bool:
        return self.device_id is not None


class ListMountedVolumesUseCase:
    """Enumera os volumes conectados e marca os que já estão registrados."""

    def __init__(
        self, volume_catalog: VolumeCatalog, device_repository: DeviceRepository
    ) -> None:
        self._catalog = volume_catalog
        self._devices = device_repository

    def execute(self) -> list[CatalogedVolume]:
        """Devolve todo volume montado, ordenado por onde está conectado.

        Uma enumeração e um `SELECT`, cruzados em memória pela identidade do
        volume -- que é uma chave, comparada por igualdade e nada mais. Sem
        ignorar maiúsculas e sem normalização: o valor é opaco acima do
        adaptador que o cunhou, e um repositório que ignorasse maiúsculas
        fundiria dois volumes numa plataforma cujos identificadores
        distinguem maiúsculas (`DeviceRepository.get_by_volume_identity`).

        Volumes que a plataforma se recusa a descrever nunca chegam aqui; o
        adaptador os pula, exatamente como o RFC-027 decidiu que um disco que
        não responde não é razão para parar de relatar os que responderam.

        Ordenado pelo ponto de montagem, porque é assim que o usuário os vê
        -- `D:`, `F:`, `G:` -- e porque `list_mounted()` não promete ordem.
        Ordenar aqui, e não no adaptador, deixa a escolha na camada que sabe
        que está sendo desenhada.
        """
        known = {device.volume_identity: device.id for device in self._devices.list()}
        cataloged = [
            CatalogedVolume(volume=volume, device_id=known.get(volume.identity))
            for volume in self._catalog.list_mounted()
        ]
        return sorted(cataloged, key=lambda entry: str(entry.volume.mount_point))
