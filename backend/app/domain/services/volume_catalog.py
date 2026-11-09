"""Perguntar à máquina o que está plugado nela, sem importar a plataforma.

A pergunta que `DeviceLocator` não consegue responder. Aquela porta recebe
um `Device` em toda assinatura, porque responde *"onde está este disco que
eu já conheço"*. `GET /api/v1/volumes` pergunta sobre a **máquina** -- sobre
volumes que ainda não são um dispositivo e talvez nunca virem um -- então
não há `Device` a passar, e um quarto método em `DeviceLocator` teria dado a
`FakeDeviceLocator` duas identidades (RFC-031 §4.2 do build prompt).

**Nada pode guardar cache entre chamadas**, pela razão escrita em
`DeviceLocator`: um ponto de montagem é um fato deste instante, o usuário
puxa o cabo, e nada avisa este processo. O provider em
`app/presentation/dependencies/` por isso monta um catálogo por requisição,
e nunca pode ganhar um `lru_cache`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.domain.value_objects.device_id import VolumeIdentity
from app.domain.value_objects.mounted_volume import MountedVolume


class VolumeCatalog(ABC):
    """Porta para enumerar os volumes ligados a esta máquina agora."""

    @abstractmethod
    def mount_points(self) -> dict[VolumeIdentity, Path]:
        """Devolve todo volume montado agora, mapeado para onde está.

        A metade barata, e a que está no caminho quente. Uma enumeração
        responde "este disco está conectado, e onde" para **todos** os
        dispositivos de uma vez: um chamador com vinte dispositivos resolve
        a lista inteira contra este único mapa, em vez de perguntar vinte
        vezes (RFC-031 §4.1).

        Os chamadores podem segurar o resultado pela duração de uma
        requisição, e não podem segurá-lo por mais tempo.
        """

    @abstractmethod
    def list_mounted(self) -> list[MountedVolume]:
        """Os mesmos volumes, cada um com o rótulo do sistema de arquivos e capacidade.

        **De propósito um segundo método, e não um primeiro mais rico.** O
        rótulo e a capacidade custam uma chamada a mais à plataforma por
        volume, e ler uma capacidade pode acordar um disco externo que estava
        dormindo -- segundos, e não milissegundos. `mount_points()` é chamado
        em toda página de busca por `ResolveImageLocationUseCase`, então
        dobrar o detalhe nele poria esse custo na frente de toda consulta do
        produto para servir uma tela que raramente é aberta (RFC-031 §4.1 do
        build prompt).

        Volumes que a plataforma se recusa a nomear são pulados, em vez de
        levantarem erro, exatamente como `mounted_volumes()` os pula: um
        drive óptico vazio e um volume BitLocker travado não são razões para
        parar de informar os discos que responderam (RFC-027 §7).

        A ordem não faz parte do contrato. Os chamadores que desenham a lista
        a ordenam pelo que exibem.
        """
