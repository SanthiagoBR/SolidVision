"""Parar um job de indexação, de forma cooperativa (RFC-029 §8).

O cancelamento tem duas formas e uma porta de entrada, e qual forma vale
depende de alguém estar segurando o job:

* um job **na fila** é cancelado de vez. Nada está rodando, então não há a quem
  pedir com educação;
* um job **em execução** recebe uma flag. O worker é dono de toda transição
  para fora de `running`, lê a flag entre lotes, termina o lote em andamento,
  grava o seu checkpoint e sai. Mudar `status` aqui, em vez disso, disputaria
  essa coluna com o worker, e liberaria o disco antes de o worker de fato o
  ter soltado.

E uma recusa: cancelar um job que já terminou levanta exceção. O RFC é
explícito: isso é um erro, e não um no-op silencioso -- quem pede isso tem uma
crença errada, e um 202 a confirmaria.

**Nada é desfeito.** Um job cancelado em 60% deixa 60% do escopo indexado e
buscável, e o próximo job sobre o mesmo escopo pula essa parte pela decisão
incremental do RFC-020. Cancelar para; não reverte.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import IllegalJobTransitionError, JobNotFoundError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.job_id import JobId


class CancelIndexingJobUseCase:
    """Pede a um job que pare, do jeito que o seu estado atual permitir."""

    def __init__(
        self,
        job_repository: IndexingJobRepository,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._jobs = job_repository
        self._clock = clock or _utc_now

    def execute(self, job_id: JobId) -> IndexingJob:
        """Cancela `job_id`, ou registra que ele deve parar, e devolve o job.

        Escrito como ler, decidir, gravar condicionalmente -- no máximo duas
        vezes, e nunca em loop. A corrida que vale nomear é a primeira: entre
        ler um job `pending` e gravá-lo `cancelled`, um executor pode
        reivindicá-lo. A gravação condicional então não casa com nenhuma
        linha, e a segunda metade deste método faz a coisa certa para o job
        como ele está agora, em vez de sobrescrever um job em execução com um
        cancelado e deixar um worker indexando um job que o banco diz ter
        acabado.

        O limite é o que faz isto terminar. Se a segunda gravação condicional
        também perde, o job terminou sozinho nesse meio-tempo -- o que é um
        `IllegalJobTransitionError` legítimo, não algo para tentar de novo.
        """
        job = self._require(job_id)
        # Pede permissão à entidade antes de tocar em qualquer coisa: um job
        # terminado levanta exceção aqui, e a rota não precisa de um `if`
        # próprio (`AI_Context.md` -- a apresentação não decide regra de
        # negócio).
        flagged = job.request_cancel()

        if job.status is JobStatus.PENDING:
            cancelled = self._jobs.save_if_status(
                job.cancel(self._clock()), JobStatus.PENDING
            )
            if cancelled is not None:
                return cancelled

            # Um worker o reivindicou no meio-tempo. Relê e o trata como o job
            # em execução que ele agora é -- inclusive levantando exceção se
            # ele já terminou, que é o que `request_cancel()` faz.
            job = self._require(job_id)
            flagged = job.request_cancel()

        if job.status is JobStatus.RUNNING:
            requested = self._jobs.save_if_status(flagged, JobStatus.RUNNING)
            if requested is not None:
                return requested

        raise IllegalJobTransitionError(
            f"Job {job_id} finished before the cancellation could be recorded."
        )

    def _require(self, job_id: JobId) -> IndexingJob:
        """Busca o job, ou diz claramente que esse job não existe.

        Diferente de "o job existe e não começou", que é um job `pending`
        comum que alguém está consultando. Juntar os dois tornaria um erro de
        digitação num id indistinguível de uma fila lenta.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(f"No indexing job with id {job_id}.")
        return job


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
