"""Porta abstrata de repositório para a persistência dos jobs de indexação (RFC-029).

Duas propriedades desta porta são incomuns no código, e as duas são
deliberadas.

**Toda escrita é condicional, e não há um `save()` simples.** Os outros
repositórios daqui são stores: o chamador é dono da entidade e a escreve de
volta. Um job não tem um dono só -- uma rota liga `cancel_requested` enquanto
um worker escreve contadores, um reaper pode tirar um job abandonado do worker
que ainda o segura, e dois workers podem estender a mão para o mesmo job
enfileirado no mesmo milissegundo. Por isso "escreva isto só se a linha ainda
estiver no estado a partir do qual decidi" é o contrato, e não um detalhe de
implementação, e um método que escrevesse incondicionalmente seria aquele que
todo chamador futuro pegaria por engano.

**Ela nunca decide nada.** As transições moram em `IndexingJob`; o que está
aqui é a garantia de que uma transição decidida é aplicada atomicamente. O
único lugar em que essa linha fica borrada é `claim_next()`, que precisa
escolher uma linha e movê-la num único comando -- e o teste de contrato prende
o resultado a `IndexingJob.claim()`, para que os dois não se afastem.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId


class IndexingJobRepository(ABC):
    """Contrato de repositório para gerenciar as entidades `IndexingJob`."""

    @abstractmethod
    def create(self, job: IndexingJob) -> IndexingJob:
        """Insere um job novo, recusando um dispositivo que já tem um ativo.

        Levanta `DeviceBusyError` quando o dispositivo já tem um job
        `pending` ou `running`. **Uma implementação não pode responder isso
        com uma consulta própria**: a implementação PostgreSQL deixa o
        índice único parcial do RFC-029 §9 recusar o insert e traduz a
        falha, porque um `SELECT` antes do `INSERT` é um check-then-act, e
        duas requisições concorrentes passariam as duas por ele.

        O dublê em memória não tem índice em que se apoiar e por isso
        precisa emular a recusa. Isso não é uma gentileza opcional: um
        dublê que aceitasse dois jobs ativos faria de todo teste de
        Application acima dele uma evidência sobre o dublê, e não sobre o
        sistema.

        Devolve o job gravado, para que o chamador veja os valores
        atribuídos pelo servidor -- `created_at` em particular -- em vez de
        supor os seus.
        """

    @abstractmethod
    def get(self, job_id: JobId) -> IndexingJob | None:
        """Busca um job, ou `None` quando nenhuma linha tem esse id."""

    @abstractmethod
    def record_progress(
        self,
        job_id: JobId,
        progress: IndexingProgress,
        checkpoint: ImagePath | None,
        heartbeat_at: datetime.datetime,
    ) -> IndexingJob | None:
        """Escreve o progresso e um heartbeat num job em execução, e o relê.

        **Uma ida e volta, e esse é o ponto.** O RFC-029 §9.1 põe o
        heartbeat na mesma escrita que já atualiza os contadores, então
        provar que o worker está vivo não custa nada a mais; e a linha que
        volta traz `cancel_requested`, então o worker fica sabendo se lhe
        pediram para parar pela mesma chamada.

        Escreve **só** essas colunas. Uma escrita da entidade inteira aqui
        levaria a cópia velha que o próprio worker tem de `cancel_requested`
        -- lida quando ele pegou o job, minutos atrás, e `False` -- direto
        por cima de um cancelamento que a rota gravou desde então. O bug é
        invisível: o job simplesmente nunca para.

        Um `checkpoint` `None` deixa o checkpoint gravado como está, em vez
        de limpá-lo: quer dizer "nada novo está durável ainda", que é como
        fica um buffer de lote cheio, e limpar jogaria fora uma posição de
        que um crash ainda precisa.

        Devolve `None` quando o job não está mais em execução -- o reaper o
        reenfileirou, ou ele foi cancelado de vez -- o que diz ao worker que
        não é mais ele quem segura este job.
        """

    @abstractmethod
    def save_if_status(
        self, job: IndexingJob, expected: JobStatus
    ) -> IndexingJob | None:
        """Escreve `job` só enquanto a linha dele ainda tiver `expected`.

        O compare-and-set de que o caminho do cancelamento é feito (RFC-029
        §8). Cancelar um job `pending` é imediato, e a corrida a que ele
        precisa sobreviver é um worker pegando esse job no instante entre a
        leitura e a escrita; o `UPDATE ... WHERE status = 'pending'` ou
        ganha, ou não casa com nenhuma linha.

        Devolve o job gravado, ou `None` quando a linha já tinha mudado --
        o que não é erro. O chamador relê e decide de novo, o que, para o
        cancelamento, quer dizer recorrer a ligar a flag num job que agora
        está em execução.
        """

    @abstractmethod
    def claim_next(self, now: datetime.datetime) -> IndexingJob | None:
        """Pega atomicamente o job enfileirado mais antigo, ou `None` se não há.

        **Não pode ser implementado como select-then-update.** Dois
        executores consultando ao mesmo tempo vão escolher a mesma linha, e
        sob `READ COMMITTED` o `UPDATE` do perdedor não casa com nada e ele
        volta a dormir com trabalho ainda na fila -- um fantasma de latência
        invisível com um executor só, que é como todo teste roda. A
        implementação PostgreSQL faz o select `FOR UPDATE SKIP LOCKED`
        dentro do `UPDATE`, e o teste de contrato dispara dois claims
        concorrentes e exige dois jobs diferentes.

        O mais antigo primeiro, por `created_at`. Não há prioridade nem
        reordenação (RFC-029 §14).

        A linha pega é exatamente `job.claim(now)` aplicado à linha como
        estava -- `started_at` preservado numa retomada, heartbeat carimbado
        na hora -- e o teste de contrato afirma essa igualdade, em vez de
        confiar que cada implementação escreveu a transição do mesmo jeito.
        """

    @abstractmethod
    def claim(self, job_id: JobId, now: datetime.datetime) -> IndexingJob | None:
        """Pega atomicamente *este* job, se ele ainda estiver na fila.

        O que a CLI usa. `indexing_worker --root PATH` cria um job e depois
        o roda no próprio processo, então precisa do mesmo movimento atômico
        do laço de consulta, mas apontado para um id -- e precisa perder com
        elegância quando um executor separado chegou antes, caso em que
        passa a acompanhar o job por consulta (RFC-029 §12).

        Devolve `None` quando o job não existe ou não está mais `pending`.
        """

    @abstractmethod
    def list_stale(self, heartbeat_before: datetime.datetime) -> list[IndexingJob]:
        """Devolve os jobs em execução com último heartbeat antes de `heartbeat_before`.

        A consulta do reaper, e só a consulta: o que *fazer* com um job
        abandonado -- de volta à fila, cancelado ou, enfim, falho -- é
        decisão de `IndexingJob.expire()`, e o reaper a aplica por
        `save_if_status()`, para que um job que voltou à vida entre as duas
        chamadas fique em paz.

        Um job em execução que nunca emitiu um heartbeat não pode existir:
        `claim()` carimba um. Uma implementação que devolvesse linhas com
        heartbeat `NULL` estaria recolhendo jobs sobre os quais não tem
        evidência.
        """

    @abstractmethod
    def list(
        self,
        device_id: DeviceId | None = None,
        status: JobStatus | None = None,
    ) -> list[IndexingJob]:
        """Lista os jobs, o mais novo primeiro, opcionalmente por dispositivo ou status.

        O mais novo primeiro faz parte do contrato, e não é uma
        conveniência: o histórico de um disco é lido para responder "o que
        aconteceu por último", e uma lista cuja ordem dependesse da
        implementação faria o dublê em memória e o PostgreSQL discordarem
        sobre a primeira linha que uma interface desenha.
        """
