"""Esquecer, por um botão, as fotos que um disco não tem mais (RFC-046 §5.7).

O único caminho por que uma linha de `images` sai do banco desde o RFC-046,
além do recomeço do RFC-045: o usuário pede, por disco, depois de saber
quantas são. O que um scan faz é marcar (`SweepMissingImagesUseCase`); isto
apaga o que está marcado.

    o pedido                            a resposta
    um dispositivo que ninguém conhece  DeviceNotFoundError -- 404
    um dispositivo com um job pendente  DeviceBusyError -- 409: o job pode
    ou rodando                          desmarcar uma linha que isto apagaria
    nos outros casos                    o número de linhas apagadas

**O disco não precisa estar plugado.** As marcas vieram de um scan que foi
conferido quando rodou; esquecer o que não está mais num disco não precisa do
disco.

**Nenhuma guarda do RFC-030.** Elas protegem o sistema de arquivos do usuário,
e nada aqui toca nele: as linhas e as miniaturas são o que esta aplicação fez.
"""

from __future__ import annotations

from app.domain.entities.indexing_job import JobStatus
from app.domain.exceptions import DeviceBusyError, DeviceNotFoundError
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.repositories.image_repository import ImageRepository
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.services.thumbnail_store_port import ThumbnailStorePort
from app.domain.value_objects.device_id import DeviceId


class PurgeMissingImagesUseCase:
    """Apaga as linhas marcadas de um dispositivo, e depois as miniaturas delas."""

    def __init__(
        self,
        image_repository: ImageRepository,
        device_repository: DeviceRepository,
        job_repository: IndexingJobRepository,
        thumbnail_store: ThumbnailStorePort,
    ) -> None:
        self._images = image_repository
        self._devices = device_repository
        self._jobs = job_repository
        self._thumbnails = thumbnail_store

    def execute(self, device_id: DeviceId) -> int:
        """Purga as linhas marcadas de `device_id` e devolve quantas saíram.

        **As linhas primeiro, numa transação, e só depois as miniaturas.** Um
        processo que morre entre as duas deixa miniaturas sem linhas, que são
        lixo no cache da aplicação; a outra ordem deixaria linhas apontando
        para nada.

        **Recusado enquanto o disco tem um job** (`DeviceBusyError`): um job
        rodando termina com uma varredura que desmarca as fotos que ele achou
        de novo, e uma foto que ele achou perderia a linha aqui -- e passaria
        de novo pela inferência no próximo scan. O job runner roda um job por
        vez, então, sem job ativo, nada mais escreve as marcas (§5.4).
        """
        device = self._devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"No device with id {device_id}.")

        for status in (JobStatus.RUNNING, JobStatus.PENDING):
            if self._jobs.list(device_id=device_id, status=status):
                raise DeviceBusyError(
                    f"Device {device.label!r} has an indexing job; wait for it "
                    "to end before removing its missing photos."
                )

        purged = self._images.purge_missing(device_id)
        for location in purged.thumbnail_paths:
            try:
                self._thumbnails.delete(location)
            except OSError:
                # Uma miniatura sendo servida neste instante não pode ser
                # removida no Windows. Ela fica como lixo -- a falha que esta
                # ordem aceita -- e as linhas, já apagadas, são a resposta.
                continue
        return purged.count
