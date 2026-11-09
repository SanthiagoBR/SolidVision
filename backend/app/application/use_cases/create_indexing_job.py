"""Aceitar o pedido de indexar um disco, ou algumas pastas dele (RFC-029 §7.1).

O caso de uso que transforma um corpo HTTP numa linha, e **é só isso que ele
faz**. Não abre arquivo, não carrega modelo e nunca chama
`IndexOrUpdateImagesUseCase`. Toda a diferença entre este RFC e o que o
RFC-026 §3 proibiu é que a requisição volta assim que a intenção é registrada,
e o trabalho acontece em outro processo.

O que ele faz antes de registrar a intenção é recusar os quatro pedidos que
nunca poderiam dar certo, cada um com um erro de domínio que a camada HTTP já
sabe responder:

* um dispositivo que ninguém nunca viu -- 404;
* um dispositivo que não está plugado *agora* -- 409, porque o mesmo pedido
  seria aceito com o disco conectado;
* um escopo que escapa do dispositivo, ou que não é uma pasta dele -- 400;
* um dispositivo que já está ocupado -- 409, e esse é recusado pelo banco, não
  aqui (ver abaixo).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable

from app.domain.entities.device import Device
from app.domain.entities.indexing_job import IndexingJob
from app.domain.exceptions import DeviceNotConnectedError, DeviceNotFoundError
from app.domain.exceptions.job_errors import InvalidJobScopeError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope, normalize_scopes


class CreateIndexingJobUseCase:
    """Valida um pedido de indexação e o põe na fila."""

    def __init__(
        self,
        job_repository: IndexingJobRepository,
        device_repository: DeviceRepository,
        device_locator: DeviceLocator,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        """Compõe o caso de uso, opcionalmente com um relógio escolhido por quem chama.

        `clock` tem como padrão o relógio de parede e existe para que um teste
        possa dizer o que é "agora" em vez de fazer asserções em volta disso. É
        um callable simples, não uma porta: a camada de aplicação precisa da
        hora atual, não de uma abstração sobre o tempo, e uma ABC de um método
        aqui seria cerimônia (`Settings` é injetado do mesmo jeito, como
        argumento e não como import).
        """
        self._jobs = job_repository
        self._devices = device_repository
        self._locator = device_locator
        self._clock = clock or _utc_now

    def execute(self, device_id: DeviceId, scopes: Iterable[str] = ()) -> IndexingJob:
        """Põe na fila um job para `device_id`, restrito a `scopes`.

        Um `scopes` vazio significa o dispositivo inteiro, que é o que zero
        linhas de escopo já significam no schema (RFC-029 §5.2).

        O job devolvido traz os escopos que serão *de fato* percorridos, não os
        que foram pedidos: pedidos sobrepostos são absorvidos, e cada um é
        reescrito com a grafia que o sistema de arquivos usa. A rota os ecoa
        no 202, para que um cliente que pediu `2018` e `2018/junho` veja que
        recebeu um só.

        **Não há verificação de job ativo existente aqui.** Essa recusa cabe ao
        índice único parcial do RFC-029 §9, e perguntar antes seria um
        check-then-act: dois pedidos simultâneos para um disco não achariam
        nada e os dois inseririam. `IndexingJobRepository.create()` levanta
        `DeviceBusyError` quando o banco recusa.
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        if self._locator.mount_point(device) is None:
            raise DeviceNotConnectedError(
                f"Device {device.label!r} is not connected. Plug it in and "
                "try again."
            )

        resolved = tuple(self._resolve(device, scope) for scope in scopes)

        return self._jobs.create(
            IndexingJob(
                id=JobId.new(),
                device_id=device.id,
                scopes=normalize_scopes(resolved),
                created_at=self._clock(),
            )
        )

    def _resolve(self, device: Device, raw: str) -> JobScope:
        """Transforma um escopo pedido na pasta que o sistema de arquivos reconhece.

        Duas recusas, nesta ordem, porque a ordem é a garantia. `JobScope`
        rejeita um caminho absoluto, uma letra de unidade, um compartilhamento
        UNC e qualquer `..` **sem tocar no disco** -- então um escopo que escapa
        é recusado exista a pasta ou não, e nunca tem uma chance de ser
        resolvido como caminho real.

        Só então o localizador olha: ele resolve o escopo sob o ponto de
        montagem, confirma que o resultado continua dentro do dispositivo --
        uma junction NTFS dentro do escopo parece uma pasta comum até ser
        resolvida -- e devolve a grafia canônica da pasta.
        """
        scope = JobScope(raw)
        resolved = self._locator.resolve_scope(device, scope)
        if resolved is None:
            raise InvalidJobScopeError(
                f"{raw!r} is not a folder on device {device.label!r}."
            )
        return resolved


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
