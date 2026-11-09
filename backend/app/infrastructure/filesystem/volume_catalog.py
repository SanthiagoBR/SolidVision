"""O adaptador de `VolumeCatalog`, sobre o provedor de volumes do RFC-027 (RFC-031 §5).

Uma casca fina sobre `VolumeIdentityProvider`, no formato que
`MountedDeviceLocator` já tem: o conhecimento da plataforma fica em
`WindowsVolumeIdentityProvider`, e nada de novo aqui sabe em que sistema
operacional está.

**Nada guarda cache, e a ausência é o desenho.** Toda chamada enumera de novo,
pela razão escrita nas duas portas: um ponto de montagem está errado a partir do
momento em que o usuário puxa o cabo, e o Windows não avisa este processo quando
isso acontece.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import VolumeIdentity
from app.domain.value_objects.mounted_volume import MountedVolume
from app.infrastructure.filesystem.volume_identity_provider import (
    ResolvedVolume,
    VolumeIdentityError,
    VolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


def as_mounted_volume(resolved: ResolvedVolume) -> MountedVolume:
    """Traduz o valor da infraestrutura para o do domínio.

    Uma função de módulo e não um método, porque o único chamador que precisa
    dela fora desta classe é `indexing_worker.register_device()`: a CLI resolve
    um volume a partir de um caminho `--root`, que é uma pergunta que este
    catálogo não faz, e depois entrega a resposta ao mesmo caso de uso da
    aplicação que a rota HTTP usa. Uma tradução, escrita uma vez, para que as
    duas portas de entrada não possam discordar sobre o que estão registrando.
    """
    return MountedVolume(
        identity=resolved.identity,
        mount_point=resolved.mount_point,
        filesystem_label=resolved.filesystem_label,
        total_bytes=resolved.total_bytes,
    )


class MountedVolumeCatalog(VolumeCatalog):
    """Responde a partir do que o sistema operacional relata como montado, sempre."""

    def __init__(self, volume_provider: VolumeIdentityProvider) -> None:
        self._volumes = volume_provider

    def mount_points(self) -> dict[VolumeIdentity, Path]:
        """Uma enumeração, direto -- a resposta do RFC-027 §7.

        Medida em 0,19 ms de mediana com um volume montado (RFC-030,
        `experiments/rfc-030-file-access/`), o que a torna acessível no caminho
        da busca e acessível uma vez por listagem de dispositivos.
        """
        return self._volumes.mounted_volumes()

    def list_mounted(self) -> list[MountedVolume]:
        """A mesma enumeração, mais uma chamada de identificação por volume.

        **Este é o método caro, e é separado por essa razão.** `resolve()` lê o
        rótulo do sistema de arquivos com `GetVolumeInformationW` e a capacidade
        com `GetDiskFreeSpaceExW`; a segunda pode fazer girar um disco externo
        que tinha estacionado as cabeças, o que leva segundos e não
        milissegundos. `mount_points()`, acima, está no caminho quente -- toda
        página de busca resolve os seus resultados por ele --, então o detalhe
        só é pago pelos dois chamadores que o pediram, `GET /volumes` e o
        registro que precisa escrever `filesystem_label` e `total_bytes` numa
        linha nova (RFC-031 §4.1 do build prompt).

        Um volume que não responde é pulado e registrado no log, nunca vira
        exceção: `mounted_volumes()` já pula os que se recusam a ser *nomeados*,
        e um disco que se recusa a ser *descrito* é a mesma situação uma
        pergunta depois -- não é razão para deixar de relatar os discos que
        responderam (RFC-027 §7).

        A identidade mantida é a da enumeração, não a que `resolve()` deriva de
        novo. São a mesma string, da mesma chamada à plataforma, e usar a da
        enumeração é o que garante que uma chave de `mount_points()` e uma
        `identity` daqui sejam iguais na comparação -- que é todo o mecanismo
        pelo qual `GET /volumes` sabe que um volume já é um dispositivo
        registrado.
        """
        mounted: list[MountedVolume] = []
        for identity, mount_point in self._volumes.mounted_volumes().items():
            try:
                resolved = self._volumes.resolve(mount_point)
            except VolumeIdentityError:
                logger.warning(
                    "Skipping volume at %s: it would not describe itself",
                    mount_point,
                )
                continue
            mounted.append(
                MountedVolume(
                    identity=identity,
                    mount_point=mount_point,
                    filesystem_label=resolved.filesystem_label,
                    total_bytes=resolved.total_bytes,
                )
            )
        return mounted
