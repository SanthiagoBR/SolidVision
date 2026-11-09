"""Marcar as fotos que um scan do disco inteiro não acha mais (RFC-046 §5.5).

Rodado pelo job runner no fim de um job do disco inteiro que terminou, com o
disco ainda onde foi listado (§5.4). Compara as linhas do dispositivo com os
arquivos daquela listagem:

    a linha                        a listagem                 a varredura
    visível                        tem o id dela              nada
    visível                        não tem o id dela          a marca
    visível, dentro de uma pasta   -- (a pasta não foi        nada: protegida
    ilegível                       lida)
    marcada                        tem o id dela              a desmarca
    marcada                        não tem o id dela          nada: mantém a data

**Ela marca e nunca apaga** (§3). Um falso positivo -- uma pasta que sumiu da
listagem por uma razão que ninguém viu -- custa um scan para desfazer, sem
inferência; uma exclusão custaria os vetores. Apagar é da purga, por um
botão.

**Os ids já chegam cunhados.** Esta camada não sabe como um arquivo vira um
id: o job runner os cunha a partir da listagem com a função que os cunha
durante a indexação (`listed_identity()`), e os entrega.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass

from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.job_scope import JobScope


@dataclass(frozen=True)
class SweepReport:
    """O que uma varredura fez, para o log do job.

    Os três números da linha de INFO (§5.5, passo 5) e, para o WARNING de cada
    pasta ilegível, quantas linhas cada uma manteve visíveis.
    """

    marked: int
    """Linhas visíveis fora da listagem, marcadas agora."""
    unmarked: int
    """Linhas marcadas que voltaram à listagem, desmarcadas agora."""
    protected: int
    """Linhas visíveis fora da listagem, mantidas visíveis por uma pasta ilegível."""
    protected_by_folder: tuple[tuple[JobScope, int], ...] = ()
    """Cada pasta ilegível, na ordem dada, com as linhas que ela protegeu."""


class SweepMissingImagesUseCase:
    """Cruza as linhas de um dispositivo com a listagem do disco; marca a diferença."""

    def __init__(
        self,
        repository: ImageRepository,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        """`clock` dá a data das marcas; por padrão, o relógio de parede.

        Um callable, e não uma porta, como o que `CreateIndexingJobUseCase`
        recebe: o job runner entrega o seu, então um teste que move a hora do
        job move também a das marcas.
        """
        self._repository = repository
        self._clock = clock or _utc_now

    def execute(
        self,
        device_id: DeviceId,
        listed: Set[ImageId],
        unreadable: Sequence[JobScope] = (),
    ) -> SweepReport:
        """Marca o que está ausente e desmarca o que voltou, numa escrita só.

        `listed` são os ids de todo arquivo que a listagem achou -- o disco
        inteiro, inclusive os arquivos antes do checkpoint de uma retomada e
        aqueles cuja indexação falhou: um arquivo que está lá não está ausente
        (§4). `unreadable` são as pastas que o percurso não conseguiu ler; uma
        linha dentro de uma delas nunca é marcada.

        **Dentro compara partes, e não texto** (§4): `2018` ilegível não
        protege `2018b/x.jpg`. É `JobScope.contains()`, a regra que o RFC-029
        §10 fixou para os escopos de um job.

        As linhas são lidas agora, no fim do job, para que as que este job
        criou também sejam comparadas (passo 1).
        """
        missing: list[ImageId] = []
        returned: list[ImageId] = []
        protected = [0] * len(unreadable)

        for row in self._repository.list_presence(device_id):
            if row.image_id in listed:
                if row.missing:
                    returned.append(row.image_id)
                continue
            if row.missing:
                continue
            folder = _first_containing(unreadable, row.relative_path)
            if folder is None:
                missing.append(row.image_id)
            else:
                protected[folder] += 1

        if missing or returned:
            self._repository.mark_presence(missing, returned, self._clock())

        return SweepReport(
            marked=len(missing),
            unmarked=len(returned),
            protected=sum(protected),
            protected_by_folder=tuple(zip(unreadable, protected, strict=True)),
        )


def _first_containing(folders: Sequence[JobScope], relative_path: str) -> int | None:
    """O índice da primeira pasta que contém `relative_path`, ou `None`.

    O caminho de uma linha só é quebrado em partes quando há uma pasta com que
    compará-lo: uma varredura sem nada ilegível -- quase toda varredura -- não
    constrói nenhum `JobScope`.
    """
    if not folders:
        return None
    parts = JobScope(relative_path)
    for index, folder in enumerate(folders):
        if folder.contains(parts):
            return index
    return None


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
