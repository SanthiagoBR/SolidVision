"""Porta de entrada HTTP dos jobs de indexação (RFC-029 §7).

**Estas rotas não indexam nada, e todo o desenho é esse.** Elas gravam
uma linha e leem linhas de volta. Nenhum arquivo é aberto aqui, nenhum
modelo é carregado, e `IndexOrUpdateImagesUseCase` nunca é alcançado a
partir deste módulo -- um processo separado, no host, consulta a tabela e
faz o trabalho.

Essa distinção é o que separa este RFC daquilo que o RFC-026 §3 proibiu.
A proibição nunca foi "a indexação não pode ser exposta por HTTP"; foi
que a indexação não pode acontecer *dentro de uma requisição*, porque uma
rota que rodasse cinco horas de inferência prenderia um worker do
threadpool por cinco horas e derrubaria o `/health` junto. A forma
assíncrona sempre foi compatível, e é esta.

O que isso exclui, e cada item é algo plausível de se usar: nada de
`BackgroundTasks`, nada de `SessionLocal`, nada de `ClipEmbeddingModel`,
nada de `torch`, nada de decidir se um cancelamento é válido, e nada de
procurar um job ativo antes de criar outro.
`tests/test_ai_layer_boundaries.py` percorre os imports deste pacote para
manter honestos os quatro primeiros; os dois últimos são decisões que
vivem no Domínio e no banco, respectivamente.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.application.use_cases.cancel_indexing_job import CancelIndexingJobUseCase
from app.application.use_cases.create_indexing_job import CreateIndexingJobUseCase
from app.application.use_cases.get_indexing_job import GetIndexingJobUseCase
from app.application.use_cases.list_indexing_jobs import ListIndexingJobsUseCase
from app.domain.entities.indexing_job import JobStatus
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.job_id import JobId
from app.infrastructure.logging.logger import get_logger
from app.presentation.dependencies import (
    get_cancel_indexing_job_use_case,
    get_create_indexing_job_use_case,
    get_indexing_job_use_case,
    get_list_indexing_jobs_use_case,
)
from app.presentation.schemas.job_schema import (
    CreateJobRequestSchema,
    JobListSchema,
    JobSchema,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSchema,
    summary="Queue an indexing run for a device, or for folders on it",
)
def create_job(
    request: CreateJobRequestSchema,
    use_case: CreateIndexingJobUseCase = Depends(get_create_indexing_job_use_case),
) -> JobSchema:
    """Enfileira um job e responde na hora com o que foi enfileirado.

    **202, não 201.** 201 afirma que o recurso pedido pelo cliente existe
    e está pronto. O que existe é a *intenção* de indexar; o resultado
    ainda não existe e vai levar minutos ou horas. 202 descreve isso, e a
    diferença é observável pelo cliente que precisa decidir se vai
    consultar de novo (RFC-029 §7.1).

    `def` síncrono, não `async def`, segundo o RFC-026 §9: tudo abaixo
    desta linha é uma chamada DBAPI bloqueante, e um `async def` a rodaria
    no event loop e travaria toda requisição concorrente -- `/health`
    inclusive -- enquanto ela durasse. O trabalho em si é de outro
    processo, então o que bloqueia aqui é um `INSERT`.

    A resposta devolve os escopos **normalizados**, que podem não ser os
    que foram enviados: `2018` e `2018/junho` são uma pasta só a
    percorrer, e um cliente que pediu as duas deve poder ver que recebeu
    uma. `[]` quer dizer o dispositivo inteiro.

    Sem `try/except`. Um dispositivo que não existe, um disco na gaveta,
    uma pasta que não está lá e um disco já ocupado são todos erros de
    domínio, e `error_handlers.py` transforma cada um em 404, 409, 400 e
    409 sem que esta função conheça nenhum desses números.
    """
    logger.info(
        "job requested: device=%s, scopes=%d", request.device_id, len(request.scopes)
    )
    job = use_case.execute(DeviceId(request.device_id), request.scopes)
    logger.info("job %s queued", job.id)
    return JobSchema.from_domain(job)


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    response_model=JobListSchema,
    summary="List indexing jobs, newest first",
)
def list_jobs(
    device_id: UUID | None = Query(
        default=None, description="Only jobs for this device; omit for all devices"
    ),
    job_status: JobStatus | None = Query(
        default=None,
        alias="status",
        description="Only jobs in this state; omit for all states",
    ),
    use_case: ListIndexingJobsUseCase = Depends(get_list_indexing_jobs_use_case),
) -> JobListSchema:
    """Devolve o histórico que uma UI mostra ao lado de um disco.

    Os dois filtros são opcionais, e um omitido quer dizer *todos*, nunca
    *nenhum* -- a armadilha que o `device_id` da rota de busca teve de
    evitar, em que um parâmetro ausente virava um `IN ()` vazio e toda
    requisição sem filtro não devolvia nada.

    Um id de dispositivo desconhecido não é recusado. O filtro estreita um
    conjunto em vez de afirmar que seus membros existem, então a lista de
    um disco que o sistema nunca viu volta vazia, e corretamente (o mesmo
    raciocínio que o RFC-027 §9 aplica aos filtros da busca).

    `job_status` se chama `status` na requisição. O parâmetro não pode se
    chamar `status` nesta assinatura porque esse é o módulo de status do
    FastAPI, importado acima e usado em todo decorator daqui.
    """
    jobs = use_case.execute(
        device_id=DeviceId(device_id) if device_id is not None else None,
        status=job_status,
    )
    return JobListSchema.from_domain(jobs)


@router.get(
    "/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=JobSchema,
    summary="Read one indexing job's progress",
)
def get_job(
    job_id: UUID,
    use_case: GetIndexingJobUseCase = Depends(get_indexing_job_use_case),
) -> JobSchema:
    """Devolve o job, ou 404 quando o id não aponta para nada.

    Polling, não WebSocket nem SSE (RFC-029 §7.2). O cliente é uma UI
    local pedindo um número que muda a cada poucos segundos; um canal
    persistente acrescentaria gestão de conexão, reconexão e um caminho
    assíncrono pela API para economizar requisições que custam quase nada
    em localhost. Fica como trabalho futuro, se uma medição algum dia
    mostrar o custo.

    Um job ausente é 404, e não um 200 vazio, porque "enfileirado, não
    iniciado" e "esse id nunca foi um job" são as duas coisas que um
    cliente que consulta periodicamente mais precisa distinguir.
    """
    return JobSchema.from_domain(use_case.execute(JobId(job_id)))


@router.post(
    "/{job_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobSchema,
    summary="Ask an indexing job to stop",
)
def cancel_job(
    job_id: UUID,
    use_case: CancelIndexingJobUseCase = Depends(get_cancel_indexing_job_use_case),
) -> JobSchema:
    """Cancela de vez um job enfileirado, ou pede a um em execução que encerre.

    **202, porque cancelar um job em execução também é um pedido, e não um
    resultado.** O worker é dono de toda transição para fora de `running`:
    ele lê a flag entre lotes, termina o lote em andamento -- até
    `settings.batch_size` imagens de inferência já paga, que abortar
    jogaria fora por nada --, grava o checkpoint e sai. Então a resposta
    pode dizer que a parada foi registrada, e um cliente que acompanha o
    `status` vê `running` com `cancel_requested` verdadeiro até o job de
    fato parar.

    Um job enfileirado não tem a quem pedir e é cancelado na hora, o que o
    mesmo formato de resposta informa.

    Cancelar um job que já terminou é um 409, não um sucesso silencioso:
    quem chama age com uma crença errada sobre o sistema, e um 202 a
    confirmaria (RFC-029 §8). Se um cancelamento é válido é decisão de
    `IndexingJob`, não desta rota.
    """
    logger.info("cancellation requested for job %s", job_id)
    return JobSchema.from_domain(use_case.execute(JobId(job_id)))
