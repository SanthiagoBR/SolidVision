"""Fazer de um volume um dispositivo que este sistema conhece (RFC-031 §6).

A regra que morava em `infrastructure/workers/indexing_worker.py` e só podia
ser alcançada por um `argparse`. Um produto cuja premissa é *"o fotógrafo
aponta para os próprios discos"* não pode exigir um terminal para um disco
existir, e o RFC-031 dá à regra um terceiro chamador que é uma rota HTTP --
e, a partir daí, a camada deixa de ser discutível.

**O que mudou de lugar, e o que de propósito não mudou.** O RFC-031 §13 diz
que `register_device()` *"migra"* para cá, e a migração literal é
impossível: a assinatura dela recebe um `VolumeIdentityProvider`, devolve um
`ResolvedVolume` e chama `compute_device_id()`, que eram três imports de
`app.infrastructure` -- e `test_application_architecture.py` quebra o build
por qualquer um deles. Então a função foi dividida na linha que já existia:

* **dar nome a um volume** fica na Infrastructure, porque os dois chamadores
  perguntam à plataforma de jeitos diferentes. A CLI tem um caminho
  (`--root D:\\fotos`) e o resolve; a rota tem uma identidade opaca e a
  procura numa enumeração. Nenhuma das duas perguntas tem resposta acima do
  adaptador;
* **decidir qual linha escrever** -- a cascata do `label`, o `first_seen_at`
  preservado, o `last_seen_at` atualizado, os contadores do scan intocados --
  vem para cá. É a parte que a docstring antiga avisava que "diverge em duas
  cópias", e é o que o RFC-031 queria na camada Application.

`indexing_worker.register_device()` continua existindo com a mesma assinatura
pública, agora uma casca que resolve uma raiz e chama `register()`, abaixo.
Seis chamadores passam `root` e `label` e leem uma tupla de volta, e nenhum
deles mudou.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.entities.device import Device
from app.domain.exceptions import (
    DeviceNotConnectedError,
    InvalidVolumeIdentityError,
)
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.services.device_identity import compute_device_id
from app.domain.services.volume_catalog import VolumeCatalog
from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind
from app.domain.value_objects.mounted_volume import MountedVolume


@dataclass(frozen=True)
class RegisteredDevice:
    """Um dispositivo registrado, e se foi esta chamada que o criou."""

    device: Device

    created: bool
    """`False` quando a identidade já tinha uma linha -- nada foi criado.

    Levado para fora do caso de uso, em vez de decidido na rota, porque a
    rota não tem como saber: `save()` é um upsert por desenho (RFC-027),
    então um segundo registro e um primeiro são a mesma escrita. Não custa
    consulta a mais -- `get_by_volume_identity()` já é chamado para preservar
    o `first_seen_at` -- e é o que deixa uma rota responder `201` da primeira
    vez e `200` da segunda (RFC-031 §6.1).
    """


class RegisterDeviceUseCase:
    """Registra um volume montado, ou atualiza o dispositivo que ele já é."""

    def __init__(
        self,
        volume_catalog: VolumeCatalog,
        device_repository: DeviceRepository,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        """Compõe o caso de uso, opcionalmente com um relógio escolhido pelo chamador.

        `clock` segue `CreateIndexingJobUseCase`: um callable simples, e não
        uma porta, para que um teste possa dizer o que é "agora" em vez de
        fazer asserções em volta disso. A camada Application precisa da hora
        atual, não de uma abstração sobre o tempo.

        `volume_catalog` é obrigatório até no caminho da CLI, que nunca toca
        nele -- `register()` recebe um volume que outro resolveu. Um objeto
        com duas entradas é mais simples que uma dependência opcional, e
        construir o catálogo custa uma referência: é uma casca sobre uma
        enumeração que ele ainda não fez.
        """
        self._catalog = volume_catalog
        self._devices = device_repository
        self._clock = clock or _utc_now

    def execute(
        self, volume_identity: str, volume_kind: str, label: str = ""
    ) -> RegisteredDevice:
        """Registra o volume montado com esta identidade; 409 se ele não está aqui.

        **A identidade é uma chave, nunca um local.** Ela é comparada por
        igualdade com um mapeamento que o próprio servidor produziu momentos
        antes; nunca é interpretada, nunca é juntada a nada e nunca vira um
        caminho. A `root` de que o registro precisa é o ponto de montagem *que
        o servidor enumerou*, e é isso que preserva a propriedade que o
        RFC-030 §5.1 estabeleceu para `/reveal`, deixando ainda o usuário
        adicionar um disco (RFC-031 §5.1). Uma identidade inventada,
        portanto, não é um lugar -- não resolve para nada, e essa é
        exatamente a diferença entre este desenho e aceitar um caminho.

        As duas strings chegam sem conversão porque convertê-las é uma regra
        de domínio com um erro de domínio junto. Declarar
        `volume_kind: VolumeKind` no schema do pedido faria o FastAPI recusar
        um tipo desconhecido com um `422` *antes* de este método rodar, e o
        `400 InvalidVolumeIdentityError` que o RFC-031 §6 especifica nunca
        seria levantado. É o argumento que `CreateJobRequestSchema` já faz
        para manter `scopes` uma lista de strings simples: o que torna um
        valor válido aqui é uma regra que esta camada detém, e respondê-la na
        Presentation produz um 422 onde cabe um 400 com uma mensagem legível.

        Uma identidade vazia não precisa de checagem própria --
        `VolumeIdentity.__post_init__` já a recusa com o mesmo erro.

        `list_mounted()` em vez de `mount_points()`: uma linha nova precisa
        registrar `filesystem_label` e `total_bytes`, e esta é a única rota em
        que pagar por eles é o objetivo.
        """
        identity = VolumeIdentity(value=volume_identity, kind=_kind(volume_kind))
        for volume in self._catalog.list_mounted():
            if volume.identity == identity:
                return self._merge(volume, label, persist=True)

        raise DeviceNotConnectedError(
            "No volume with that identity is mounted right now. Plug the "
            "disk in and try again."
        )

    def register(
        self, volume: MountedVolume, label: str, persist: bool = True
    ) -> Device:
        """Aplica a regra de fusão a um volume que o chamador já resolveu.

        A entrada da CLI: `indexing_worker.register_device()` pergunta à
        plataforma sobre um caminho de `--root`, que é uma pergunta que nenhum
        cliente HTTP pode fazer, e depois chega aqui com a resposta. Nada
        neste método toca o sistema operacional.

        `persist=False` calcula o dispositivo sem escrevê-lo, que é do que
        `device_reconcile --dry-run` precisa: um dry run que criasse uma linha
        não seria um dry run, e o id do dispositivo é derivado, e não
        alocado, então nada precisa ser escrito para que ele seja conhecido.
        """
        return self._merge(volume, label, persist).device

    def _merge(
        self, volume: MountedVolume, label: str, persist: bool
    ) -> RegisteredDevice:
        """Funde um volume montado na linha que ele deve ter, e diz se ela é nova.

        As regras sobre quais campos sobrevivem de uma linha existente, num
        lugar só, porque duas cópias delas divergem e os dois chamadores
        cunham ids de imagem a partir do dispositivo que produzem:

        * `label` prefere o que o chamador pediu, depois o nome que o usuário
          já deu ao disco, depois o rótulo do próprio volume, depois a string
          da identidade. Um disco já conhecido mantém o nome, a menos que
          alguém tenha pedido para mudá-lo -- renomear é decisão do usuário,
          não efeito colateral da indexação;
        * `first_seen_at` é preservado e `last_seen_at` avança a cada chamada,
          o que faz do par um histórico, e não um estado;
        * `last_scan_at` e `last_scan_file_count` passam adiante intocados.
          São o resultado de um scan, e registrar um disco não é um --
          sobrescrevê-los aqui zeraria o denominador do número de "% indexado"
          toda vez que alguém abrisse a barra lateral (RFC-031 §4.2).

        `filesystem_label` e `total_bytes` vêm do volume, em vez de serem
        preservados: são observações, e a observação mais recente vence.
        """
        now = self._clock()
        existing = self._devices.get_by_volume_identity(volume.identity)

        device = Device(
            id=compute_device_id(volume.identity),
            volume_identity=volume.identity,
            label=(
                label
                or (existing.label if existing else None)
                or volume.filesystem_label
                or volume.identity.value
            ),
            filesystem_label=volume.filesystem_label,
            total_bytes=volume.total_bytes,
            first_seen_at=existing.first_seen_at if existing else now,
            last_seen_at=now,
            last_scan_at=existing.last_scan_at if existing else None,
            last_scan_file_count=existing.last_scan_file_count if existing else None,
        )
        if persist:
            self._devices.save(device)
        return RegisteredDevice(device=device, created=existing is None)


def _kind(value: str) -> VolumeKind:
    """Lê um discriminador de plataforma; um desconhecido é erro de domínio.

    `VolumeKind(value)` levanta `ValueError`, que nenhum handler traduz e que
    apareceria como um 500. `InvalidVolumeIdentityError` é o erro que o
    RFC-031 §6 nomeia, já existe e já responde 400.
    """
    try:
        return VolumeKind(value)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in VolumeKind))
        raise InvalidVolumeIdentityError(
            f"{value!r} is not a volume kind this system knows. Expected one "
            f"of: {known}."
        ) from exc


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
