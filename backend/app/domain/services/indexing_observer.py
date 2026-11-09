"""A junção pela qual uma indexação longa dá notícias, e é parada.

`IndexOrUpdateImagesUseCase` não tinha ponto de extensão nenhum antes do
RFC-029: recebia um iterável, rodava até o iterável se esgotar e devolvia
contadores. As duas metades do RFC-029 -- o progresso durante a execução
(§7.3) e o cancelamento cooperativo (§8) -- precisam de um, e é este.

**A porta não diz nada sobre jobs.** Nenhum heartbeat, nenhum banco, nenhum
`cancel_requested`, nenhum id. É a invariante 3 do build prompt em forma de
porta: a decisão incremental não pode ficar sabendo que jobs existem, ou "a
CLI reindexa coisas que a interface pula" passa a ser possível. O adaptador
da Infrastructure é onde um `should_stop()` vira a leitura de uma flag e um
`batch_persisted()` vira um `UPDATE` no máximo a cada N segundos.

A alternativa -- entregar `IndexingJobRepository` ao caso de uso -- foi
recusada exatamente por essa razão.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.root_listing import RootListing


class IndexingObserver(ABC):
    """Acompanha uma indexação e pode lhe pedir para parar.

    As implementações não podem levantar. Uma exceção de qualquer método
    daqui aparece dentro do pipeline, onde abortaria a execução por uma razão
    que nada tem a ver com as imagens -- e, levantada durante o consumo
    preguiçoso do gerador da descoberta, ela fecha esse gerador e trunca o
    scan em silêncio, que é a falha que `discovered_candidates()` já
    documenta.
    """

    @abstractmethod
    def discovered(self, files_seen: int, complete: bool) -> None:
        """Informa quantos arquivos o scan produziu até agora.

        Chamado de dentro do fluxo da descoberta, e é por isso que existe
        separado dos dois callbacks abaixo: um scan de um disco mecânico a
        frio, ou uma retomada que pula tudo antes do checkpoint, pode rodar
        por minutos sem completar uma única janela, e uma execução que só
        informasse por lote pareceria morta durante todo esse tempo (RFC-029
        §9.1).

        `complete` é verdadeiro exatamente uma vez, quando o scan se esgota.
        Até lá, `files_seen` é uma contagem corrente, e não um total -- não
        há total, porque a descoberta é um gerador (RFC-021), e contar antes
        seria ler o disco duas vezes.
        """

    def listed(self, listing: RootListing) -> None:
        """Informa o que a listagem da raiz inteira contém.

        Chamado uma vez, por um scan de uma raiz inteira -- sem escopos --
        assim que a listagem termina e antes de o primeiro arquivo sair
        (RFC-039 §4.1). Não é `discovered()`: aquele conta o que o scan
        *produziu até agora*, e só sabe o total no fim da execução, enquanto
        este é a listagem que o scan já tem em memória, conhecida antes de
        qualquer arquivo ser lido. Numa retomada, inclui os arquivos que o
        checkpoint pula, porque a listagem é sempre da árvore inteira.

        Desde o RFC-046, traz a própria listagem, e não só o tamanho dela: os
        arquivos, relativos à raiz, e as pastas que não puderam ser lidas
        (`RootListing`). O tamanho é o denominador do "% indexado"; os
        arquivos são aquilo com que a varredura ao fim do job compara as
        linhas do dispositivo, para marcar as que não estão mais no disco
        (RFC-046 §5.3). Só arquivos contam: o percurso distingue uma pasta
        chamada `x.jpg` de um arquivo sem um `stat` (§5.2).

        Um scan de algumas pastas nunca o chama: os arquivos de duas pastas
        não são o disco.

        **Não abstrato, e vazio aqui**, para que o observador nulo e todo
        dublê de teste escrito antes do RFC-039 continuem funcionando sem
        mudança. Só o observador de um job tem uso para ele.
        """
        return None

    @abstractmethod
    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        """Informa o fim de uma janela de prefetch de metadados.

        A contraparte de `batch_persisted` para o caso que não produz lote
        nenhum: um novo scan de um disco já indexado decide janelas inteiras
        como `SKIP_UNCHANGED` e nunca descarrega nada. É a forma de execução
        mais comum que existe, e uma execução que só informasse na descarga
        não se distinguiria de uma morta.
        """

    @abstractmethod
    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        """Informa que as linhas de um lote foram gravadas com commit.

        `durable_through` é o checkpoint, e é o arquivo até o qual *tudo*
        está durável -- não o último arquivo escrito. Ele pode andar para
        trás em relação ao caminho processado mais novo, e pode ser `None`
        enquanto o buffer pendente guarda o arquivo ainda não decidido mais
        antigo. As duas coisas estão certas: um checkpoint que passasse à
        frente do buffer descartaria essas imagens em silêncio numa
        retomada.
        """

    @abstractmethod
    def should_stop(self) -> bool:
        """Se a execução deve se encerrar no próximo ponto seguro.

        Consultado entre lotes e entre janelas, nunca dentro de uma passada
        do modelo. Uma execução que para descarrega o que já inferiu e volta
        normalmente -- parar não é falha, e descartar inferência que já foi
        paga não compra nada (RFC-029 §8).
        """


class NullIndexingObserver(IndexingObserver):
    """O observador de todo chamador que não quer um.

    O argumento padrão de `IndexOrUpdateImagesUseCase.execute()`, que é o que
    deixa toda a suíte de testes existente do RFC-024 continuar passando sem
    mudança: uma execução sem ninguém olhando se comporta exatamente como
    antes do RFC-029, e `should_stop()` respondendo `False` para sempre é o
    antigo laço incondicional.
    """

    def discovered(self, files_seen: int, complete: bool) -> None:
        return None

    def window_decided(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def batch_persisted(
        self, progress: IndexingProgress, durable_through: ImagePath | None
    ) -> None:
        return None

    def should_stop(self) -> bool:
        return False
