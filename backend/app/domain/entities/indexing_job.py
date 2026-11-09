"""A entidade do job de indexação e a máquina de estados que detém as transições.

Um job é um pedido para indexar um dispositivo, ou algumas pastas dele. É um
*evento*, e não uma descrição do disco: dois pedidos para indexar a mesma
pasta são duas linhas, e a linha guarda os contadores e o checkpoint muito
depois de o trabalho acabar (RFC-029 §5.1).

**Todo movimento legal mora aqui, e em nenhum outro lugar.** A rota não
decide se um cancelamento é permitido, e o worker não decide se um job
expirado pode voltar para a fila -- os dois chamam um método desta entidade e
a deixam recusar (`AI_Context.md`: nenhuma regra de negócio na Presentation
ou na Infrastructure). O que a Infrastructure detém é a *atomicidade* de um
movimento, que é outra pergunta e mora no repositório.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass, replace

from app.domain.exceptions import IllegalJobTransitionError
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope


class JobStatus(enum.StrEnum):
    """Em que ponto da vida um job está.

    Um `StrEnum`, para que o membro *seja* a string que a coluna do banco
    guarda. A coluna é `TEXT`, e não um `ENUM` do PostgreSQL, pela razão que o
    RFC-028 deu para `capture_source`: acrescentar um estado depois não deveria
    custar uma migration.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Se nada mais vai acontecer a um job neste estado."""
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """Se este estado segura o dispositivo.

        Exatamente os estados que o índice único parcial do RFC-029 §9 cobre,
        nomeados uma vez para que o índice, o dublê em memória e o filtro da
        lista não possam divergir. `running` é óbvio; `pending` é o que vale
        dizer, e é deliberado -- um job na fila já reservou o disco, então um
        segundo pedido para o mesmo disco é recusado, e não posto na fila atrás
        dele.
        """
        return self in _ACTIVE


_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
_ACTIVE = frozenset({JobStatus.PENDING, JobStatus.RUNNING})


class JobEvent(enum.StrEnum):
    """Tudo o que se pode pedir a um job, nomeado para que os testes o enumerem.

    Existe porque "as transições legais estão testadas" é uma afirmação mais
    fraca do que parece: verificar que os movimentos legais funcionam não diz
    nada sobre os ilegais, e uma entidade que permitisse `complete` num job
    cancelado passaria numa suíte assim. Com os eventos como dados,
    `test_indexing_job_transitions.py` percorre a grade inteira de
    `(status, event)` e exige uma recusa em toda célula fora da tabela abaixo.
    """

    CLAIM = "claim"
    COMPLETE = "complete"
    FAIL = "fail"
    CANCEL = "cancel"
    RELEASE = "release"
    EXPIRE = "expire"
    REQUEST_CANCEL = "request_cancel"


LEGAL_TRANSITIONS: dict[JobEvent, frozenset[JobStatus]] = {
    JobEvent.CLAIM: frozenset({JobStatus.PENDING}),
    JobEvent.COMPLETE: frozenset({JobStatus.RUNNING}),
    JobEvent.FAIL: frozenset({JobStatus.RUNNING}),
    JobEvent.CANCEL: frozenset({JobStatus.PENDING, JobStatus.RUNNING}),
    JobEvent.RELEASE: frozenset({JobStatus.RUNNING}),
    JobEvent.EXPIRE: frozenset({JobStatus.RUNNING}),
    JobEvent.REQUEST_CANCEL: frozenset({JobStatus.PENDING, JobStatus.RUNNING}),
}
"""De quais estados cada evento é permitido. A máquina de estados inteira.

Lida como uma tabela, e não espalhada por comandos `if`, para que a resposta
a "um job cancelado pode ser concluído?" seja uma consulta só e o teste
exaustivo tenha o que enumerar.

Duas entradas valem as palavras:

* `FAIL` só é alcançável a partir de `RUNNING`. Um job que nunca começou não
  pode ter descoberto que o disco está numa gaveta -- o executor revalida a
  conexão *depois* de pegar o job, e a essa altura o job está rodando.
* `EXPIRE` também é só de `RUNNING`, porque um job pendente não tem
  heartbeat para perder.
"""


@dataclass(frozen=True)
class IndexingJob:
    """Um pedido de indexação, o seu progresso e de onde retomá-lo.

    Congelada, e toda transição devolve um valor novo. Isso segue `Image` e
    `Device` (RFC-009), e aqui se paga de um jeito que lá não se paga: um job
    é escrito por uma rota que põe `cancel_requested` e por um worker que
    escreve contadores ao mesmo tempo, e uma entidade que mudasse no lugar
    faria de "qual desses eu acabei de sobrescrever?" uma pergunta invisível.
    Um valor novo tem de ser entregue a um repositório, que é onde essa
    concorrência é de fato resolvida.
    """

    id: JobId
    device_id: DeviceId
    status: JobStatus = JobStatus.PENDING
    scopes: tuple[JobScope, ...] = ()
    """As pastas a percorrer, já normalizadas; `()` para o dispositivo inteiro.

    Normalizadas por `normalize_scopes()` antes de chegarem aqui, para que
    nada mais adiante tenha de lidar com um escopo aninhado em outro (RFC-029
    §5.2).
    """

    created_at: datetime.datetime | None = None
    started_at: datetime.datetime | None = None
    """Quando este job foi pego *pela primeira vez*, e não da última.

    Preservado numa retomada, de propósito. `created_at -> started_at` é a
    latência da fila que o RFC-029 §6.1 manda medir, e reescrevê-lo toda vez
    que um worker retoma o job faria um job que caiu duas vezes parecer ter
    começado logo.
    """

    finished_at: datetime.datetime | None = None
    progress: IndexingProgress = IndexingProgress()
    last_processed_relative_path: ImagePath | None = None
    """O checkpoint: todo arquivo até este, inclusive, está durável.

    **Não é "o último caminho visto".** O coordenador de lotes guarda os
    arquivos decididos num buffer pendente que sobrevive entre janelas, então
    um caminho posterior pode estar decidido enquanto um anterior ainda não
    foi escrito; um checkpoint tirado do último caminho visto passaria por
    cima desse buffer, e uma queda perderia exatamente as imagens que estão
    nele. Ver `IndexOrUpdateImagesUseCase.execute()`.

    Relativo ao dispositivo, como tudo o que é persistido desde o RFC-027. Um
    checkpoint absoluto deixaria de valer no momento em que o disco voltasse
    com outra letra de unidade.
    """

    error_message: str | None = None
    last_heartbeat_at: datetime.datetime | None = None
    cancel_requested: bool = False
    """Ligado pela rota; lido pelo worker entre os lotes.

    Uma flag, e não um estado `cancelling`, porque o worker é o único dono
    das transições que saem de `running`. Um estado poria a rota e o worker
    numa corrida pela coluna `status`, e liberaria o índice único parcial
    antes de o worker ter de fato soltado o disco (RFC-029 §8; o schema do
    rascunho, em §5.1, omitia esta coluna por completo).
    """

    attempts: int = 0
    """Quantas vezes um worker recebeu este job e não o terminou.

    Incrementado só por `expire()`. Uma parada limpa não conta, e a primeira
    pegada também não: o número responde a "quantas vezes algo matou um worker
    que segurava este job", e existe para interromper o único caso que, fora
    isso, rodaria em laço para sempre -- um arquivo cujo decodificador derruba
    o processo inteiro, matando todo worker que retoma sobre ele.
    """

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, IndexingJob):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def claim(self, now: datetime.datetime) -> IndexingJob:
        """Tira o job da fila e começa a rodá-lo.

        `started_at` só é escrito da primeira vez. O heartbeat é carimbado na
        hora, e não no primeiro lote, porque um worker que pegou um job e
        depois passou dois minutos percorrendo um disco frio não pode parecer
        abandonado antes de ter emitido qualquer coisa (RFC-029 §9.1).
        """
        self._require(JobEvent.CLAIM)
        return replace(
            self,
            status=JobStatus.RUNNING,
            started_at=self.started_at or now,
            last_heartbeat_at=now,
        )

    def complete(self, now: datetime.datetime) -> IndexingJob:
        """Termina com sucesso.

        O chamador tem de ter confirmado que o dispositivo continua montado
        antes de pedir isto (RFC-029 §15). A entidade não consegue checar --
        o Domain não sabe o que é um ponto de montagem --, e é por isso que a
        regra é enunciada no executor e testada lá.
        """
        self._require(JobEvent.COMPLETE)
        return replace(self, status=JobStatus.COMPLETED, finished_at=now)

    def fail(self, now: datetime.datetime, message: str) -> IndexingJob:
        """Termina sem sucesso, registrando por quê.

        Não toca em `attempts`. Um job que falhou por uma razão em que uma
        nova tentativa esbarraria de novo -- um disco desplugado, um escopo
        que sumiu -- não consumiu uma vida, porque nada vai tentá-lo de novo: o
        RFC-029 §14 deixa `failed` fora da retomada automática, e o usuário o
        recria.
        """
        self._require(JobEvent.FAIL)
        return replace(
            self,
            status=JobStatus.FAILED,
            finished_at=now,
            error_message=message,
        )

    def cancel(self, now: datetime.datetime) -> IndexingJob:
        """Para a pedido do usuário, mantendo tudo o que já foi indexado.

        Legal a partir de `pending`, onde a rota o faz direto porque nenhum
        worker segura o job, e de `running`, onde só o worker o faz, e só
        depois que o lote em andamento terminou. Cancelar não desfaz: um job
        cancelado em 60% deixa 60% do escopo buscável, e o próximo job sobre o
        mesmo escopo pula essa parte pela decisão incremental (RFC-029 §8).
        """
        self._require(JobEvent.CANCEL)
        return replace(self, status=JobStatus.CANCELLED, finished_at=now)

    def release(self, now: datetime.datetime) -> IndexingJob:
        """Devolve o job à fila depois de uma parada *limpa*.

        O `Ctrl+C` no executor chega como `KeyboardInterrupt`, que é um
        operador parando um processo, e não uma queda. O job volta para
        `pending` com o checkpoint intacto e **nenhuma tentativa consumida**:
        contá-la significaria que reiniciar o worker três vezes por razões
        comuns -- um reboot, uma mudança de configuração -- faria falhar para
        sempre um job que nunca deu errado.

        `now` é aceito e, de propósito, não é escrito em lugar nenhum; em vez
        disso, o heartbeat é limpo, porque ninguém trabalha num job liberado e
        um heartbeat novo afirmaria que alguém trabalha. Manter o parâmetro faz
        toda transição receber o relógio, e nenhum chamador precisa lembrar
        quais precisam dele.
        """
        self._require(JobEvent.RELEASE)
        del now
        return replace(self, status=JobStatus.PENDING, last_heartbeat_at=None)

    def expire(self, now: datetime.datetime, max_attempts: int) -> IndexingJob:
        """Resolve um job cujo worker parou de provar que estava vivo.

        Três desfechos, e a ramificação está aqui, e não no reaper, porque é
        uma regra sobre o que um job *é*, e não sobre como ele é encontrado:

        * um job que alguém já tinha pedido para cancelar vira `cancelled`. O
          worker se foi, então não sobra nada para parar, e a resposta do
          usuário continua sendo a certa -- relatar `failed` culparia o sistema
          por uma parada que o usuário escolheu;
        * um job com tentativas sobrando volta para `pending`, mantendo o
          checkpoint, para que um worker retome em vez de recomeçar. É isso
          que dá à coluna do checkpoint um consumidor: marcar o job `failed` e
          deixar o usuário recriá-lo cunha uma linha nova com checkpoint
          `NULL`, que relê o escopo inteiro (RFC-029 §9.1, corrigido);
        * um job que esgotou `max_attempts` vira `failed`. Sem esse limite, um
          arquivo que derruba o processo de vez mataria, para sempre, todo
          worker que retomasse sobre ele.

        O dispositivo continua reservado no caso `pending`, e é esse o ponto:
        o índice único parcial cobre `pending` também, então um job
        interrompido em 60% mantém o seu lugar, em vez de perder o disco para
        o que quer que tenha sido pedido em seguida.
        """
        self._require(JobEvent.EXPIRE)
        if self.cancel_requested:
            return replace(self, status=JobStatus.CANCELLED, finished_at=now)

        attempts = self.attempts + 1
        if attempts >= max_attempts:
            return replace(
                self,
                status=JobStatus.FAILED,
                finished_at=now,
                attempts=attempts,
                error_message=(
                    f"Heartbeat expired {attempts} time(s); the worker holding "
                    "this job is presumed dead and no attempts remain."
                ),
            )
        return replace(
            self,
            status=JobStatus.PENDING,
            attempts=attempts,
            last_heartbeat_at=None,
        )

    def request_cancel(self) -> IndexingJob:
        """Registra que o usuário quer este job parado.

        Recusa num job que já terminou. O RFC-029 §8 é explícito: isso é um
        erro, e não uma operação nula silenciosa -- um chamador que pede para
        cancelar um job `completed` tem uma crença errada sobre o sistema, e
        um 202 a confirmaria.

        Não recebe relógio, porque não é uma transição -- o status não se
        move, e quem acaba por movê-lo é o worker.
        """
        self._require(JobEvent.REQUEST_CANCEL)
        return replace(self, cancel_requested=True)

    def with_progress(
        self,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob:
        """Registra até onde a execução chegou, e que ela continua viva.

        Não é uma transição: o status não se move e nenhuma checagem de
        legalidade se aplica. O heartbeat viaja com os contadores porque os
        dois são escritos juntos, que é o que o RFC-029 §9.1 quer dizer com
        "no additional round trip" -- mas *quando* essa escrita acontece é uma
        política de tempo do observer da Infrastructure, e não deste método.

        O checkpoint é passado, e não derivado. Só o coordenador de lotes sabe
        quais arquivos estão duráveis, e `None` quer dizer que ele ainda não
        consegue nomear um -- tudo o que foi decidido até aqui ainda está no
        buffer pendente dele --, e nesse caso o checkpoint anterior fica, em
        vez de ser limpo.
        """
        return replace(
            self,
            progress=progress,
            last_processed_relative_path=(
                checkpoint
                if checkpoint is not None
                else self.last_processed_relative_path
            ),
            last_heartbeat_at=heartbeat_at,
        )

    def _require(self, event: JobEvent) -> None:
        """Recusa `event`, a menos que o status atual o permita."""
        if self.status not in LEGAL_TRANSITIONS[event]:
            raise IllegalJobTransitionError(
                f"Cannot {event.value} a job that is {self.status.value}."
            )
