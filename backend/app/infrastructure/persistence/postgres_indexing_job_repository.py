"""Implementação em PostgreSQL do repositório de jobs de indexação (RFC-029).

Três coisas aqui sustentam o resto e são fáceis de errar por pouco.

**A reivindicação é um comando só.** `claim_next()` é um `UPDATE` cujo `WHERE`
escolhe a linha numa subconsulta `SELECT ... FOR UPDATE SKIP LOCKED`. Escrito
como um `SELECT` seguido de um `UPDATE`, dois executores consultando no mesmo
instante escolhem o mesmo id; sob `READ COMMITTED`, o `UPDATE` do perdedor não
casa com nada e ele volta a dormir com trabalho ainda na fila. Não é
corrupção, é um fantasma de latência, e é invisível com um executor só -- que
é como todo teste roda, a não ser que se escreva um para isso.

**A violação do índice único é reconhecida pelo nome.** `create()` captura
`IntegrityError` e procura `uq_one_active_job_per_device` na constraint que o
driver nomeia. Não no texto da mensagem, que é traduzido e reescrito entre
versões, e não em "qualquer `IntegrityError`", que relataria uma chave
estrangeira de dispositivo inválida como um disco ocupado.

**Todo timestamp vem do relógio de quem chama, e `now()` nunca é usado.**
`now()` no PostgreSQL é o *início da transação*, que é a armadilha que o
RFC-029 §4.11 nomeia: com um commit por chamada, ele coincide hoje com o
relógio de parede, e uma refatoração posterior para unit of work quebraria
essa coincidência sem um único teste falhar. O build prompt propôs
`clock_timestamp()` no lugar; esta implementação usa o relógio de quem chama,
por duas razões que o prompt não tinha como pesar:

* os heartbeats que seguem uma reivindicação são escritos por `save()` a
  partir de uma entidade que o worker já montou, então são valores do relógio
  do Python, faça este método o que fizer. Gerar o *primeiro* no banco poria
  dois relógios numa coluna, e o teste de job parado compara valores dentro
  dessa coluna;
* o reaper tem de ser testável sem dormir (a própria tabela de validação do
  RFC-029 pede um worker que "morreu"), e um relógio fornecido por quem chama
  é um relógio que um teste pode adiantar.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.exceptions import DeviceBusyError
from app.domain.repositories.indexing_job_repository import IndexingJobRepository
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.infrastructure.database.models.indexing_job_model import (
    ACTIVE_JOB_INDEX,
    IndexingJobModel,
)


class PostgresIndexingJobRepository(IndexingJobRepository):
    """Implementação do repositório sobre o PostgreSQL, via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, job: IndexingJob) -> IndexingJob:
        """Insere o job, deixando o índice único parcial recusar um dispositivo ocupado.

        **Não há `SELECT` antes deste `INSERT`, e não pode haver.** Verificar
        um job ativo no Python e depois inserir é um verifica-depois-age:
        duas requisições simultâneas para o mesmo disco não acham nada e
        inserem as duas. O índice do RFC-029 §9 é onde essa corrida é de fato
        decidida, e o único trabalho deste método é traduzir a recusa dele
        num erro de domínio, para que a rota não precise de um `try/except`
        próprio (RFC-026 §8).

        O rollback na falha segue o `PostgresImageRepository`: um insert
        recusado não pode deixar a sessão num estado de rollback pendente que
        faça falhar a *próxima* escrita, sem relação com ele.
        """
        model = IndexingJobModel.from_domain(job)
        try:
            self._session.add(model)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if _violates(exc, ACTIVE_JOB_INDEX):
                raise DeviceBusyError(
                    f"Device {job.device_id} already has an active indexing job."
                ) from exc
            raise
        except Exception:
            self._session.rollback()
            raise

        return model.to_domain()

    def get(self, job_id: JobId) -> IndexingJob | None:
        """A linha como ela está agora -- nunca uma cópia que esta sessão guardou.

        **Toda leitura desta classe pede `populate_existing`**, e a razão é
        um defeito que a medição do RFC-045 achou. As sessões do job runner e
        da CLI são criadas com `expire_on_commit=False`, então um model que
        esta sessão escreveu fica no identity map dela, com os valores que
        tinha então, enquanto estiver vivo -- e um model de job é mantido
        vivo além da última referência pelo ciclo com os seus escopos, até o
        coletor de lixo calhar de rodar. Sem `populate_existing`,
        `Session.get()` e um `select` do ORM devolvem essa cópia em vez da
        linha, `with_for_update` incluído. A CLI cria um job e o reivindica
        numa sessão só, então o primeiro `record_progress()` dela podia ler o
        `pending` que o `create()` deixou ali, concluir que o job tinha sido
        tomado por outro, e deixá-lo `running` para sempre enquanto a CLI o
        acompanhava. Acontecer ou não dependia do coletor.
        """
        model = self._session.get(
            IndexingJobModel, job_id.value, populate_existing=True
        )
        return model.to_domain() if model is not None else None

    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        """Escreve as colunas de progresso de um job rodando, e lê a linha de volta.

        De propósito, **não** `model.apply(job)`. Isso escreveria cada coluna
        a partir da cópia do job que o próprio worker tem -- inclusive o
        `cancel_requested` que ele leu ao reivindicar a linha, que é `False`
        e tem minutos -- por cima de um cancelamento que a rota gravou
        nesse meio-tempo. O job então nunca pararia, e nada pareceria errado
        em lugar nenhum.

        A linha é relida `FOR UPDATE` para que a atualização e a leitura de
        `cancel_requested` sejam um passo só, e é isso que deixa o worker
        saber que foi cancelado na mesma ida e volta que prova que ele está
        vivo (RFC-029 §9.1).
        """
        try:
            model = self._session.get(
                IndexingJobModel,
                job_id.value,
                with_for_update=True,
                populate_existing=True,
            )
            if model is None or model.status != JobStatus.RUNNING.value:
                self._session.rollback()
                return None

            model.discovered_files = progress.discovered_files
            model.processed_images = progress.processed_images
            model.skipped_images = progress.skipped_images
            model.failed_images = progress.failed_images
            model.discovery_complete = progress.discovery_complete
            model.last_heartbeat_at = heartbeat_at
            if checkpoint is not None:
                model.last_processed_relative_path = str(checkpoint)

            self._session.commit()
            return model.to_domain()
        except Exception:
            self._session.rollback()
            raise

    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        """Escreve o job só enquanto a linha dele ainda estiver em `expected`.

        O compare-and-set por trás do cancelamento e do reaper. Os dois
        decidem a partir de um valor que apenas leram, e os dois precisam
        perder com elegância: um worker pode reivindicar um job `pending` no
        instante entre a leitura e a escrita, e um job abandonado pode voltar
        à vida entre a consulta do reaper e o veredito dele.

        A linha é relida `FOR UPDATE`, em vez de atualizada por um simples
        `UPDATE ... WHERE status = :expected`, porque a entidade carrega
        todas as colunas e escrevê-la pelo model mantém um único lugar que
        sabe como um job é guardado -- `IndexingJobModel.apply()`. O lock da
        linha é o que faz da verificação e da escrita um passo só.
        """
        try:
            model = self._session.get(
                IndexingJobModel,
                job.id.value,
                with_for_update=True,
                populate_existing=True,
            )
            if model is None or model.status != expected.value:
                self._session.rollback()
                return None
            model.apply(job)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return job

    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        """Reivindica atomicamente o job mais antigo na fila, pula linhas travadas."""
        candidate = (
            select(IndexingJobModel.id)
            .where(IndexingJobModel.status == JobStatus.PENDING.value)
            .order_by(IndexingJobModel.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._claim(candidate, now)

    def claim(self, job_id: JobId, now: datetime.datetime) -> IndexingJob | None:
        """Reivindica, atomicamente, um job nomeado, se ele ainda estiver na fila.

        A porta de entrada da CLI (RFC-029 §12): `indexing_worker --root
        PATH` cria um job e o roda no próprio processo, então precisa do
        mesmo movimento atômico do loop de polling, apontado para um id.
        Devolver `None` quando outro executor chegou antes não é falha -- a
        CLI então acompanha aquele job, consultando.
        """
        candidate = (
            select(IndexingJobModel.id)
            .where(
                IndexingJobModel.id == job_id.value,
                IndexingJobModel.status == JobStatus.PENDING.value,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return self._claim(candidate, now)

    def _claim(
        self, candidate: Select[tuple[uuid.UUID]], now: datetime.datetime
    ) -> IndexingJob | None:
        """Roda o `UPDATE` da reivindicação e devolve a linha que ele moveu, se houver.

        **Este SQL espelha `IndexingJob.claim()`, e tem de espelhar.** Uma
        reivindicação atômica é, por definição, um comando só, então a
        transição não pode ser calculada no Python a partir de uma linha
        lida antes. A duplicação é deliberada, e é mantida sob controle pelo
        teste de contrato, que afirma que o que volta é igual a
        `job.claim(now)` aplicado à linha como ela estava -- então as duas
        não podem se afastar em silêncio.

        `COALESCE` é o `self.started_at or now` de `claim()`: um job retomado
        mantém o instante em que começou pela primeira vez, porque
        `created_at -> started_at` é a latência de fila que o RFC-029 §6.1
        mede, e um job que caiu duas vezes não pode parecer ter começado
        prontamente.

        `synchronize_session=False` porque a entidade é remontada abaixo a
        partir do id devolvido, lido com `populate_existing`: uma cópia que
        esta sessão ainda guarda -- o `pending` que o próprio `create()` da
        CLI deixou -- é sobrescrita pela linha, e não levada em conta (veja
        `get()`).
        """
        statement = (
            update(IndexingJobModel)
            .where(IndexingJobModel.id == candidate.scalar_subquery())
            .values(
                status=JobStatus.RUNNING.value,
                started_at=func.coalesce(IndexingJobModel.started_at, now),
                last_heartbeat_at=now,
            )
            .returning(IndexingJobModel.id)
            .execution_options(synchronize_session=False)
        )
        try:
            claimed_id = self._session.execute(statement).scalar_one_or_none()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        if claimed_id is None:
            return None
        model = self._session.get(IndexingJobModel, claimed_id, populate_existing=True)
        return model.to_domain() if model is not None else None

    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        """Devolve os jobs rodando cujo heartbeat não se moveu recentemente.

        `last_heartbeat_at IS NOT NULL` faz parte da consulta, em vez de
        ficar implícito nela. `claim()` sempre grava um heartbeat, então uma
        linha rodando sem um não pode existir -- e, se um dia existisse,
        encerrá-la seria agir sem evidência nenhuma, o oposto do que um
        heartbeat existe para fazer.
        """
        statement = (
            select(IndexingJobModel)
            .where(
                IndexingJobModel.status == JobStatus.RUNNING.value,
                IndexingJobModel.last_heartbeat_at.is_not(None),
                IndexingJobModel.last_heartbeat_at < heartbeat_before,
            )
            .order_by(IndexingJobModel.created_at)
        )
        models = (
            self._session.execute(statement.execution_options(populate_existing=True))
            .scalars()
            .all()
        )
        return [model.to_domain() for model in models]

    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Devolve os jobs que casam, mais novos primeiro, como exige o contrato."""
        statement = select(IndexingJobModel).order_by(
            IndexingJobModel.created_at.desc()
        )
        if device_id is not None:
            statement = statement.where(IndexingJobModel.device_id == device_id.value)
        if status is not None:
            statement = statement.where(IndexingJobModel.status == status.value)

        models = (
            self._session.execute(statement.execution_options(populate_existing=True))
            .scalars()
            .all()
        )
        return [model.to_domain() for model in models]


def _violates(error: IntegrityError, constraint: str) -> bool:
    """Diz se `error` é a constraint nomeada recusando, e não outra.

    Lê os diagnósticos estruturados do `psycopg` em vez da string da
    mensagem. A mensagem é traduzida e já foi reescrita entre versões do
    PostgreSQL, então casar com ela transformaria um 409 de dispositivo
    ocupado num 500 no dia em que um servidor rodasse em outro locale. O
    fallback para o texto renderizado existe só para drivers que não expõem
    diagnósticos, e é um último recurso, não o mecanismo.
    """
    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    if name is not None:
        return bool(name == constraint)
    return constraint in str(error)
