"""Caso de uso da aplicação que coordena a indexação incremental de muitas imagens.

É aqui que os lotes do RFC-024 de fato acontecem, e este caso de uso existe
porque `IndexOrUpdateImageUseCase.execute()` não tinha, pela sua estrutura, como
abrigá-los: aquele método recebe uma imagem e faz a verificação de pulo, o
embedding e a persistência dessa imagem numa só chamada. Chamá-lo N vezes em
loop para "encher um lote" continuaria sendo N inferências sequenciais de uma
imagem, com contabilidade extra em volta.

As etapas são explícitas e compostas como um fluxo, não como filas ou threads
(RFC-024 §10):

    candidatos -> janela de leitura antecipada -> decisão de pulo
               -> lote de inferência -> thumbnails -> persistência

Continua sendo uma thread só aqui, desde o RFC-045. A thread que lê os arquivos
à frente deste fluxo fica na *fonte* dos candidatos, na infraestrutura
(`ReadAheadCandidates`): o que chega a esta classe é um candidato que pode
trazer os bytes do seu arquivo (`IndexCandidate.content`), que ela entrega ao
modelo e à thumbnail e libera assim que o arquivo não precisa mais deles.

Dois tamanhos de janela aparecem abaixo, e são diferentes de propósito. A
leitura antecipada de metadados lê um punhado de escalares por linha, então
quer uma janela grande; a inferência segura pixels decodificados em memória,
então quer uma pequena. Ver `Settings.metadata_prefetch_size` para o argumento
completo.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import islice

from app.application.use_cases.capture_date_plan import capture_date_to_write
from app.application.use_cases.indexing_plan import (
    IndexAction,
    IndexCandidate,
    IndexPlan,
    plan_indexing,
)
from app.application.use_cases.position_plan import position_to_write
from app.application.use_cases.thumbnail_writer import ThumbnailWriter
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.content_hasher_port import ContentHasherPort
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.services.indexing_observer import (
    IndexingObserver,
    NullIndexingObserver,
)
from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.index_metadata import IndexMetadata
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.indexing_record import IndexingRecord
from app.domain.value_objects.position import PositionReading


@dataclass(frozen=True)
class IndexingFailure:
    """Um arquivo que não pôde ser indexado, e o erro que o parou.

    Carrega a exceção original, nunca um invólucro. O RFC-024 §6 é explícito:
    a nova tentativa individual precisa mostrar o erro real e o caminho
    culpado; um invólucro genérico não diria a um operador nada sobre o que
    ele pudesse agir.
    """

    path: str
    error: BaseException


@dataclass(frozen=True)
class BatchFallback:
    """Um lote que falhou como unidade e foi tentado de novo item a item.

    Registrado porque, do contrário, a exceção do lote se perde: quando a nova
    tentativa dá certo para todos os itens, a execução não relata falha
    nenhuma, e o único rastro de que algo deu errado seria o tempo perdido.

    Usado nas duas dimensões de lote -- inferência e persistência --, que
    resolvem a mesma tensão do mesmo jeito: tentar o lote e, na falha, pagar o
    preço sequencial uma vez, para que só o item de fato ruim se perca.
    """

    paths: tuple[str, ...]
    error: BaseException


@dataclass
class IndexingSummary:
    """Contadores e tempos de uma execução de indexação.

    Legível por máquina como está -- os campos são os dados -- e apresentado
    para humanos por `format_report()`. Devolvido em vez de logado, para que a
    camada de aplicação fique livre de dependência de logging; quem chama, na
    infraestrutura, decide o que fazer com ele (RFC-024 §11).
    """

    discovered: int = 0
    skipped_unchanged: int = 0
    skipped_content_identical: int = 0
    indexed: int = 0
    inference_batches: int = 0
    inference_seconds: float = 0.0
    persistence_batches: int = 0
    persistence_writes: int = 0
    persistence_seconds: float = 0.0
    capture_dates_written: int = 0
    """Linhas já indexadas que ganharam data de captura sem refazer o embedding.

    Grande no primeiro scan depois do RFC-028 e zero em todo scan seguinte de
    uma coleção inalterada. Um valor diferente de zero num novo scan de uma
    coleção em que ninguém mexeu significaria que a gravação condicional
    deixou de ser condicional, e é por isso que ele é relatado em vez de
    ficar implícito.
    """
    positions_written: int = 0
    """Linhas já indexadas que ganharam posição sem refazer o embedding (RFC-032).

    Contado à parte de `capture_dates_written`, embora os dois venham de uma
    leitura de cabeçalho: as duas gravações são decididas separadamente, e uma
    linha pode precisar de uma e não da outra -- examinada para data antes do
    RFC-032, nunca examinada para posição. A mesma leitura do vizinho: grande
    no primeiro scan depois do RFC-032, zero em todo scan seguinte de uma
    coleção inalterada.
    """
    thumbnails_written: int = 0
    thumbnail_seconds: float = 0.0
    """Renderizar e guardar thumbnails, mantido à parte do tempo de inferência.

    À parte porque o RFC-030 §7.2 perguntou quanto as thumbnails mexem na
    vazão do RFC-024, e, somada a `inference_seconds`, a resposta ficaria
    ilegível -- o img/s do modelo no relatório passaria, em silêncio, a
    incluir um codificador JPEG.
    """
    thumbnail_failures: list[IndexingFailure] = field(default_factory=list)
    """Imagens indexadas com sucesso cuja thumbnail não pôde ser feita.

    **Não ficam em `failures`, e não são contadas por `failed`.** Cada uma
    dessas imagens ganhou embedding, foi persistida e é buscável; ela mostra
    um placeholder em vez de uma imagem até o backfill de thumbnails rodar
    (RFC-030 §7.2). Relatá-la como arquivo com falha diria a um job que uma
    foto se perdeu quando nada se perdeu.
    """
    candidate_wait_seconds: float = 0.0
    """Tempo gasto esperando o próximo candidato (RFC-045 §5.8).

    Sem a leitura antecipada, o tempo da própria descoberta -- o percurso, o
    `stat` e a leitura do EXIF. Com ela, o tempo que o pipeline esperou pela
    thread de leitura: quando é grande, o disco é o que limita a execução, e
    um modelo mais rápido não a encurtaria.
    """
    contents_prefetched: int = 0
    """Candidatos cujos bytes vieram da leitura antecipada (RFC-045 §5.8).

    Todos eles foram lidos do disco, a decisão precisando deles ou não: zero
    num novo scan de um disco inalterado, e o número de arquivos mexidos num
    novo scan de um disco em que alguns foram mexidos.
    """
    contents_read_late: int = 0
    """Arquivos cujo hash foi lido da sua localização (RFC-045 §5.8).

    Sem a leitura antecipada, todo arquivo que passou pelo hash. Com ela, os
    que a thread não leu -- uma previsão que errou, um arquivo acima do limite
    por arquivo, um orçamento que estava cheio -- e que, por isso, foram lidos
    aqui, em série com o modelo.
    """
    elapsed_seconds: float = 0.0
    failures: list[IndexingFailure] = field(default_factory=list)
    inference_fallbacks: list[BatchFallback] = field(default_factory=list)
    persistence_fallbacks: list[BatchFallback] = field(default_factory=list)
    stopped: bool = False
    """Se um observer pediu à execução que encerrasse antes do fim do fluxo.

    Não é uma falha, e deliberadamente não é uma exceção (RFC-029 §8). Uma
    execução cancelada deixa tudo o que indexou no lugar e buscável, e a
    próxima execução sobre o mesmo escopo pula isso pela decisão incremental.
    A flag é como quem chama distingue "terminou o escopo" de "parou no meio
    do caminho", que são duas coisas diferentes a escrever num job.
    """

    @property
    def failed(self) -> int:
        """Quantos arquivos individuais não puderam ser indexados."""
        return len(self.failures)

    def as_progress(self) -> IndexingProgress:
        """Projeta, dos contadores que um humano lê, os que um observador precisa.

        As duas formas são separadas de propósito. Esta classe carrega tempos,
        registros de fallback e cada falha individual porque uma execução
        terminada é relatada a uma pessoa; `IndexingProgress` carrega quatro
        inteiros porque um cliente está desenhando uma barra de progresso.
        Entregar este objeto à porta do domínio significaria também o domínio
        importar a aplicação, o que nada aqui faz.

        Os dois tipos de pulo são um número só para quem observa. "Inalterado"
        e "o mtime mudou, os bytes não" são uma distinção que tem sentido no
        relatório que o RFC-024 §11 especifica, e nenhuma distinção para
        alguém esperando uma barra andar.

        `discovery_complete` fica falso aqui, sempre. Esta classe conta o que
        o *pipeline* consumiu, e não tem como saber se o fluxo que a alimenta
        acabou -- só o scan sabe disso, e o diz por
        `IndexingObserver.discovered()`.
        """
        return IndexingProgress(
            discovered_files=self.discovered,
            processed_images=self.indexed,
            skipped_images=self.skipped_unchanged + self.skipped_content_identical,
            failed_images=self.failed,
        )

    def format_report(self) -> str:
        """Apresenta a execução como o bloco que o RFC-024 §11 especifica.

        A porcentagem da persistência não é enfeite. O RFC-024 §7.2 condiciona
        os upserts em massa a o tempo de banco ainda ser uma fração visível de
        uma execução depois que a leitura antecipada obrigatória de metadados
        está no lugar, e esta é a linha que responde a isso a partir de uma
        execução real, em vez de por afirmação.
        """
        average_batch = self._average(self.inference_seconds, self.inference_batches)
        average_write = self._average(
            self.persistence_seconds, self.persistence_batches
        )
        database_share = self._share(self.persistence_seconds, self.elapsed_seconds)
        average_thumbnail = self._average(
            self.thumbnail_seconds, self.thumbnails_written
        )

        return "\n".join(
            [
                "Indexing finished",
                "",
                f"Discovered:     {self.discovered:6d}",
                f"Skipped:        {self.skipped_unchanged:6d}   (unchanged)",
                f"Skipped (hash): {self.skipped_content_identical:6d}   "
                "(mtime changed, content identical)",
                f"Indexed:        {self.indexed:6d}",
                f"Failed:         {self.failed:6d}",
                f"Dated:          {self.capture_dates_written:6d}   "
                "(capture date written, no re-embed)",
                f"Placed:         {self.positions_written:6d}   "
                "(position written, no re-embed)",
                "",
                "Inference:",
                f"  images:       {self.indexed:6d}",
                f"  batches:      {self.inference_batches:6d}",
                f"  avg batch:    {average_batch:6.3f}s",
                f"  throughput:   "
                f"{self._rate(self.indexed, self.inference_seconds):6.1f} img/s",
                f"  fallbacks:    {len(self.inference_fallbacks):6d}   "
                "(batch failed, retried per image)",
                "",
                "Thumbnails:",
                f"  written:      {self.thumbnails_written:6d}",
                f"  failed:       {len(self.thumbnail_failures):6d}   "
                "(image indexed, no thumbnail)",
                f"  avg:          {average_thumbnail:6.3f}s",
                f"  total:        {self.thumbnail_seconds:6.2f}s",
                "",
                "Reading:",
                f"  prefetched:   {self.contents_prefetched:6d}   "
                "(bytes from the read-ahead)",
                f"  read late:    {self.contents_read_late:6d}   "
                "(hashed from the file)",
                f"  waiting:      {self.candidate_wait_seconds:6.2f}s   "
                "(for the next file)",
                "",
                "Persistence:",
                f"  rows:         {self.persistence_writes:6d}",
                f"  batches:      {self.persistence_batches:6d}",
                f"  avg write:    {average_write:6.3f}s",
                f"  fallbacks:    {len(self.persistence_fallbacks):6d}   "
                "(bulk failed, degraded to per-row)",
                f"  total:        {self.persistence_seconds:6.2f}s   "
                f"({database_share:.1f}% of elapsed)",
                "",
                "Total:",
                f"  elapsed:      {self.elapsed_seconds:6.2f}s",
                f"  throughput:   "
                f"{self._rate(self.discovered, self.elapsed_seconds):6.1f} img/s",
            ]
        )

    @staticmethod
    def _average(total: float, count: int) -> float:
        return total / count if count else 0.0

    @staticmethod
    def _rate(count: int, seconds: float) -> float:
        return count / seconds if seconds > 0.0 else 0.0

    @staticmethod
    def _share(part: float, whole: float) -> float:
        return 100.0 * part / whole if whole > 0.0 else 0.0


class DurableFrontier:
    """Até onde uma execução pode dizer que chegou, dado o que ainda não foi gravado.

    **O checkpoint não é "o último arquivo processado", e a diferença é perda
    de dados.** O buffer do lote sobrevive de uma janela de leitura antecipada
    para outra: um arquivo decidido `EMBED` na janela 1 pode ainda estar em
    `pending` enquanto arquivos da janela 3 já foram pulados e contabilizados.
    Um checkpoint tirado do último arquivo *visto* ficaria, portanto, depois de
    arquivos que nunca foram gravados, e uma queda retomaria além deles -- em
    silêncio, e para sempre, porque o scan retomado nunca mais olha para eles.

    Daí a regra (RFC-029 §10, tornada precisa): o checkpoint é o maior caminho
    `P` tal que todo arquivo descoberto até `P`, inclusive, foi gravado, pulado
    ou registrado como falha. Na prática, é o arquivo imediatamente *anterior*
    ao plano mais antigo ainda no buffer, ou o último arquivo visto quando não
    há nada no buffer.

    Errar é assimétrico, e isto erra para o lado seguro. Um checkpoint
    atrasado custa um novo scan de uns poucos arquivos cujo trabalho a decisão
    incremental então pula à toa; um checkpoint adiantado perde fotografias.

    Uma nuance, aceita e declarada: um arquivo pulado conta como durável antes
    de a sua data de captura e a sua posição serem gravadas, porque essas
    gravações acontecem uma vez por janela (RFC-028 §6, RFC-032 §8). Uma
    retomada depois dele deixa a linha com `capture_source` ou
    `position_source` NULL -- o que significa *nunca examinada*, então o
    próximo scan completo ou o `exif_backfill` a grava. Uma gravação de
    metadados adiada, não perdida.
    """

    def __init__(self) -> None:
        self._last_seen: ImagePath | None = None
        self._buffered_after: ImagePath | None = None
        self._buffering = False

    def decided(self, path: ImagePath) -> None:
        """Registra um arquivo resolvido -- pulado, atualizado ou com falha."""
        self._last_seen = path

    def buffered(self, path: ImagePath) -> None:
        """Registra um arquivo que entrou no lote e ainda não foi gravado.

        A fronteira congela no arquivo anterior a este, e fica ali até o lote
        ser descarregado -- quantas janelas isso levar.
        """
        if not self._buffering:
            self._buffered_after = self._last_seen
            self._buffering = True
        self._last_seen = path

    def flushed(self) -> None:
        """Registra que o lote foi gravado, liberando a fronteira congelada.

        Chamado depois da descarga, tenha toda linha dela sobrevivido ou não:
        uma linha que falhou foi registrada como falha, o que contabiliza o
        seu arquivo tanto quanto uma gravação bem-sucedida.
        """
        self._buffering = False
        self._buffered_after = None

    @property
    def durable_through(self) -> ImagePath | None:
        """O checkpoint agora, ou `None` quando nada é durável ainda.

        `None` é uma resposta de verdade, não uma ausência de resposta -- uma
        execução cujo primeiríssimo arquivo foi direto para o lote não tem nada
        durável atrás de si --, e quem chama deve tratá-lo como "deixe o
        checkpoint guardado como está", nunca como "apague-o".
        """
        return self._buffered_after if self._buffering else self._last_seen


class IndexOrUpdateImagesUseCase:
    """Indexa um fluxo de candidatos em lotes sem abrir mão do isolamento de erros.

    O isolamento por arquivo sobrevive à passagem para lotes (RFC-024 §6). Um
    lote é tentado como unidade; se ele levanta exceção, exatamente as mesmas
    imagens são tentadas de novo uma a uma, então N-1 delas ainda chegam ao
    banco e a de fato culpada é relatada com o seu caminho real e a sua exceção
    real. O hash e a persistência são protegidos por arquivo pelo mesmo motivo,
    e, desde o RFC-030, a renderização de uma thumbnail também.
    """

    def __init__(
        self,
        repository: ImageRepository,
        embedding_model: EmbeddingModelPort,
        content_hasher: ContentHasherPort,
        batch_size: int,
        metadata_prefetch_size: int,
        thumbnail_writer: ThumbnailWriter | None = None,
    ) -> None:
        """Compõe o pipeline.

        `thumbnail_writer` é opcional para que toda composição escrita antes do
        RFC-030 -- os benchmarks, os scripts de medição, os testes de fluxo de
        controle -- continue fazendo exatamente o que mediu. O executor que
        roda os jobs reais passa um (`build_runner()`), e um pipeline sem ele
        persiste `thumbnail_path = NULL`, que é o estado que o backfill existe
        para consertar.
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if metadata_prefetch_size < 1:
            raise ValueError(
                "metadata_prefetch_size must be at least 1, "
                f"got {metadata_prefetch_size}"
            )

        self._repository = repository
        self._embedding_model = embedding_model
        self._content_hasher = content_hasher
        self._batch_size = batch_size
        self._metadata_prefetch_size = metadata_prefetch_size
        self._thumbnail_writer = thumbnail_writer

    def execute(
        self,
        candidates: Iterable[IndexCandidate],
        observer: IndexingObserver | None = None,
    ) -> IndexingSummary:
        """Roda o pipeline inteiro sobre `candidates` e devolve o que aconteceu.

        `candidates` é consumido sob demanda e nunca materializado por
        inteiro, então a memória fica limitada pelos dois tamanhos de janela,
        não importa quantos arquivos foram descobertos.

        `observer` é opcional e tem como padrão um que não faz nada, para que
        todo chamador escrito antes do RFC-029 -- e todo teste desta classe --
        se comporte exatamente como antes. Ele é a costura de que o RFC-029
        precisa para o progresso (§7.3) e para o cancelamento cooperativo
        (§8), e é de propósito uma porta do domínio que não sabe nada de jobs:
        este método não pode aprender que jobs existem, ou "a CLI reindexa o
        que a interface pula" vira algo que pode acontecer.

        `should_stop()` é consultado entre janelas e depois de cada descarga,
        nunca no meio de uma passada do modelo. Uma parada descarrega o que já
        foi inferido -- jogar fora inferência já paga não compra nada --,
        marca `summary.stopped` e volta normalmente. Um cancelamento não é uma
        falha e não levanta exceção.
        """
        watcher = observer if observer is not None else NullIndexingObserver()
        summary = IndexingSummary()
        started = time.perf_counter()
        pending: list[IndexPlan] = []
        frontier = DurableFrontier()

        for window in windowed(
            _timed(candidates, summary), self._metadata_prefetch_size
        ):
            if watcher.should_stop():
                summary.stopped = True
                _release(window)
                break

            summary.discovered += len(window)
            summary.contents_prefetched += sum(
                1 for candidate in window if candidate.content is not None
            )
            existing = self._repository.get_index_metadata_many(
                [candidate.image.id for candidate in window]
            )
            capture_dates: list[tuple[IndexCandidate, CaptureDate]] = []
            positions: list[tuple[IndexCandidate, PositionReading]] = []

            for position_in_window, candidate in enumerate(window):
                plan = self._plan(candidate, existing, summary)
                if plan is None:
                    # Relatado como falha, o que contabiliza o arquivo: um
                    # checkpoint pode passar dele, porque retomar só falharia
                    # nele de novo.
                    frontier.decided(candidate.image.relative_path)
                    _release([candidate])
                    continue

                if plan.action is IndexAction.EMBED:
                    frontier.buffered(candidate.image.relative_path)
                    pending.append(plan)
                    if len(pending) == self._batch_size:
                        self._flush(pending, summary)
                        pending = []
                        frontier.flushed()
                        watcher.batch_persisted(
                            summary.as_progress(), frontier.durable_through
                        )
                        if watcher.should_stop():
                            summary.stopped = True
                            _release(window[position_in_window + 1 :])
                            break
                    continue

                # Pulado ou atualizado: os bytes já cumpriram o seu papel.
                _release([candidate])
                frontier.decided(candidate.image.relative_path)
                if plan.action is IndexAction.SKIP_UNCHANGED:
                    summary.skipped_unchanged += 1
                else:
                    self._refresh_metadata(plan, summary)

                stored = existing[candidate.image.id]
                capture = capture_date_to_write(
                    stored.capture_source, candidate.image.capture_date
                )
                if capture is not None:
                    capture_dates.append((candidate, capture))
                position = position_to_write(
                    stored.position_source, candidate.image.position_reading
                )
                if position is not None:
                    positions.append((candidate, position))

            self._write_capture_dates(capture_dates, summary)
            self._write_positions(positions, summary)
            watcher.window_decided(summary.as_progress(), frontier.durable_through)

            if summary.stopped:
                break

        if pending:
            self._flush(pending, summary)
            frontier.flushed()
            watcher.batch_persisted(summary.as_progress(), frontier.durable_through)

        summary.elapsed_seconds = time.perf_counter() - started
        return summary

    def _plan(
        self,
        candidate: IndexCandidate,
        existing: dict[ImageId, IndexMetadata],
        summary: IndexingSummary,
    ) -> IndexPlan | None:
        """Decide um candidato, transformando uma falha de hash numa falha relatada.

        O hash lê o arquivo, então pode falhar exatamente pelos motivos pelos
        quais a indexação pode -- um arquivo apagado entre a descoberta e o
        processamento, uma mudança de permissão, uma mídia ilegível --, e isso
        não pode abortar a execução, assim como um JPEG corrompido não aborta.

        Um arquivo que passa pelo hash aqui sem bytes da leitura antecipada é
        contado em `contents_read_late`, a leitura tendo dado certo ou não: de
        um jeito ou de outro, ele foi lido em série com o modelo (RFC-045
        §5.8).
        """
        try:
            plan = plan_indexing(
                candidate,
                existing.get(candidate.image.id),
                self._content_hasher,
            )
        except Exception as exc:
            if candidate.content is None:
                summary.contents_read_late += 1
            summary.failures.append(
                IndexingFailure(path=str(candidate.image.display_path), error=exc)
            )
            return None

        if candidate.content is None and plan.action is not IndexAction.SKIP_UNCHANGED:
            summary.contents_read_late += 1
        return plan

    def _flush(self, plans: list[IndexPlan], summary: IndexingSummary) -> None:
        """Codifica um lote montado e persiste o que sobreviveu a ele.

        O lote de persistência é o lote de inferência, não um buffer de tamanho
        próprio. O RFC-024 §7.2 exigiu que isso fosse uma decisão declarada, e
        não um acaso, e o motivo de acoplar os dois é que as linhas a gravar
        são exatamente a saída do lote recém-codificado: segurá-las para
        encher um lote de persistência maior manteria vivos N embeddings de
        512 floats a mais sem ganho medido, e quebraria a propriedade de que o
        trabalho de um lote é durável antes de o próximo começar -- que é o que
        torna barato reiniciar depois de uma queda, com o pulo incremental
        (§13). Se uma medição futura favorecer o desacoplamento, `Settings` é
        onde entra o segundo botão.

        As thumbnails são renderizadas entre os dois, para que a linha seja
        gravada uma vez já com a localização da thumbnail, em vez de gravada e
        depois atualizada -- uma segunda ida e volta por lote para um valor
        conhecido antes da primeira.

        **Os bytes do lote são liberados aqui, seja como ele terminou**
        (RFC-045 §5.5): depois da gravação, tenha toda linha chegado ao banco
        ou nenhuma. Depois deste ponto, nada os lê de novo -- uma linha que
        falhou é relatada, não tentada de novo.
        """
        try:
            encoded = self._encode_batch(plans, summary)
            thumbnails = self._render_thumbnails(encoded, summary)
            self._persist_batch(encoded, thumbnails, summary)
        finally:
            _release([plan.candidate for plan in plans])

    def _encode_batch(
        self, plans: list[IndexPlan], summary: IndexingSummary
    ) -> list[tuple[IndexPlan, EmbeddingVector]]:
        """Cronometra um lote, seja qual for o jeito como ele acaba codificado."""
        started = time.perf_counter()
        try:
            return self._encode_group(plans, summary)
        finally:
            summary.inference_seconds += time.perf_counter() - started
            summary.inference_batches += 1

    def _encode_group(
        self, plans: list[IndexPlan], summary: IndexingSummary
    ) -> list[tuple[IndexPlan, EmbeddingVector]]:
        if len(plans) == 1:
            # Um lote de um não tem custo fixo por chamada a amortizar, e
            # tentá-lo primeiro como lote só rodaria a mesma chamada falha
            # duas vezes antes de relatar o mesmo erro.
            return self._encode_individually(plans, summary)

        images = [plan.candidate.image for plan in plans]
        try:
            embeddings = self._embedding_model.encode_images(
                images, [_bytes_of(plan.candidate) for plan in plans]
            )
        except Exception as exc:
            summary.inference_fallbacks.append(
                BatchFallback(
                    paths=tuple(
                        str(plan.candidate.image.display_path) for plan in plans
                    ),
                    error=exc,
                )
            )
            return self._encode_individually(plans, summary)

        if len(embeddings) != len(plans):
            # Uma violação de contrato num adaptador, não um arquivo de
            # entrada ruim. Recuar para imagem a imagem aqui a esconderia
            # para sempre, então esta para a execução.
            raise ValueError(
                f"{type(self._embedding_model).__name__}.encode_images returned "
                f"{len(embeddings)} embeddings for {len(plans)} images"
            )

        return list(zip(plans, embeddings, strict=True))

    def _encode_individually(
        self, plans: list[IndexPlan], summary: IndexingSummary
    ) -> list[tuple[IndexPlan, EmbeddingVector]]:
        """Restaura o isolamento exato por arquivo, pagando o preço sequencial."""
        encoded: list[tuple[IndexPlan, EmbeddingVector]] = []
        for plan in plans:
            try:
                embedding = self._embedding_model.encode_image(
                    plan.candidate.image, _bytes_of(plan.candidate)
                )
            except Exception as exc:
                summary.failures.append(
                    IndexingFailure(
                        path=str(plan.candidate.image.display_path), error=exc
                    )
                )
                continue
            encoded.append((plan, embedding))
        return encoded

    def _render_thumbnails(
        self,
        encoded: list[tuple[IndexPlan, EmbeddingVector]],
        summary: IndexingSummary,
    ) -> dict[ImageId, str]:
        """Renderiza uma thumbnail para cada imagem que sobreviveu à inferência.

        **Só as sobreviventes.** `encoded` é o que `_encode_batch()` devolveu,
        o que exclui toda imagem cujo embedding falhou -- inclusive as que só
        falharam na nova tentativa imagem a imagem. Renderizá-las pagaria uma
        decodificação completa por uma linha que nunca é gravada.

        **Só `EMBED`.** Nada chega a este método para um arquivo que a decisão
        de pulo deixou passar: um arquivo inalterado já tem a thumbnail que os
        seus bytes merecem, ou não tem nenhuma e cabe ao backfill renderizá-la.
        Renderizar aqui para arquivos pulados transformaria um novo scan, que
        custa um `stat` por arquivo, numa decodificação completa por arquivo
        (RFC-030 §7.2).

        **Uma falha é relatada e nunca propagada** (RFC-030 §7.2): o embedding
        foi pago e a imagem é buscável. A imagem é persistida sem localização
        de thumbnail, e a falha vai para `thumbnail_failures`, nunca para
        `failures`.

        Uma imperfeição conhecida, aceita: o arquivo é guardado antes de a
        linha ser gravada. Se a linha então não persistir, o arquivo guardado
        fica órfão até a próxima execução para aquela imagem sobrescrevê-lo.
        """
        if self._thumbnail_writer is None or not encoded:
            return {}

        thumbnails: dict[ImageId, str] = {}
        started = time.perf_counter()
        try:
            for plan, _ in encoded:
                image = plan.candidate.image
                try:
                    thumbnails[image.id] = self._thumbnail_writer.write(
                        image, _bytes_of(plan.candidate)
                    )
                except Exception as exc:
                    summary.thumbnail_failures.append(
                        IndexingFailure(path=str(image.display_path), error=exc)
                    )
        finally:
            summary.thumbnail_seconds += time.perf_counter() - started
        summary.thumbnails_written += len(thumbnails)
        return thumbnails

    def _persist_batch(
        self,
        encoded: list[tuple[IndexPlan, EmbeddingVector]],
        thumbnails: dict[ImageId, str],
        summary: IndexingSummary,
    ) -> None:
        """Grava um lote numa transação, recuando para linha a linha se falhar.

        "Uma transação por lote" e "isolar erros por arquivo" de fato
        conflitam: um lote transacional estrito descarta todas as linhas quando
        uma delas viola uma restrição, jogando fora embeddings que custaram
        tempo real de inferência. O RFC-024 §7.2 resolve isso exatamente como a
        §6 resolve a mesma tensão para a inferência -- tentar o lote e, quando
        ele falha, pagar o preço sequencial uma vez, para que só a linha ruim
        se perca.
        """
        if not encoded:
            return

        records = [
            IndexingRecord(
                image=plan.candidate.image,
                embedding=embedding,
                file_size=plan.candidate.file_size,
                file_modified_at=plan.candidate.file_modified_at,
                content_hash=plan.content_hash,
                thumbnail_path=thumbnails.get(plan.candidate.image.id),
            )
            for plan, embedding in encoded
        ]

        started = time.perf_counter()
        try:
            self._repository.save_indexed_many(records)
        except Exception as exc:
            summary.persistence_seconds += time.perf_counter() - started
            summary.persistence_fallbacks.append(
                BatchFallback(
                    paths=tuple(str(record.image.display_path) for record in records),
                    error=exc,
                )
            )
            self._persist_individually(records, summary)
            return

        summary.persistence_seconds += time.perf_counter() - started
        summary.persistence_batches += 1
        summary.persistence_writes += len(records)
        summary.indexed += len(records)

    def _persist_individually(
        self, records: list[IndexingRecord], summary: IndexingSummary
    ) -> None:
        for record in records:
            started = time.perf_counter()
            try:
                self._repository.save_indexed(record)
            except Exception as exc:
                summary.failures.append(
                    IndexingFailure(path=str(record.image.display_path), error=exc)
                )
                continue
            finally:
                summary.persistence_seconds += time.perf_counter() - started
                summary.persistence_batches += 1

            summary.persistence_writes += 1
            summary.indexed += 1

    def _refresh_metadata(self, plan: IndexPlan, summary: IndexingSummary) -> None:
        """Registra o novo mtime e o tamanho de um arquivo cujos bytes não mudaram."""
        started = time.perf_counter()
        try:
            self._repository.update_index_metadata(
                plan.candidate.image.id,
                IndexMetadata(
                    file_size=plan.candidate.file_size,
                    file_modified_at=plan.candidate.file_modified_at,
                    content_hash=plan.content_hash,
                ),
            )
        except Exception as exc:
            summary.failures.append(
                IndexingFailure(path=str(plan.candidate.image.display_path), error=exc)
            )
            return
        finally:
            summary.persistence_seconds += time.perf_counter() - started
            summary.persistence_batches += 1

        summary.persistence_writes += 1
        summary.skipped_content_identical += 1

    def _write_capture_dates(
        self,
        capture_dates: list[tuple[IndexCandidate, CaptureDate]],
        summary: IndexingSummary,
    ) -> None:
        """Data as linhas que esta janela pulou, numa gravação, recuando por linha.

        **Os dois ramos que pulam alimentam isto, não só `SKIP_UNCHANGED`.**
        `REFRESH_METADATA` -- o mtime mudou, os bytes não -- é o ramo fácil de
        esquecer, e esquecê-lo deixaria todo arquivo já copiado ou restaurado
        sem data de captura para sempre: os scans seguintes dele são
        `SKIP_UNCHANGED` contra uma linha que continua sem data, e a condição
        abaixo teria sido a única chance. `EMBED` não passa por aqui, porque
        `save_indexed_many()` grava a linha inteira a partir da entidade,
        data de captura incluída.

        Quais linhas são gravadas é decisão de `capture_date_to_write()`: só
        as nunca examinadas, então o primeiro scan depois do RFC-028 data a
        coleção e todo scan seguinte de uma coleção inalterada não grava nada
        (RFC-028 §6).

        Uma chamada em massa por janela de leitura antecipada, e não por
        arquivo, e uma gravação em massa que falha é tentada de novo linha a
        linha, para que uma linha ruim custe uma data e não a janela -- a mesma
        troca que `_persist_batch()` faz. Contado em `capture_dates_written` e
        mantido fora dos contadores de persistência, cujas médias o RFC-024
        §7.2 lê como o custo de gravar embeddings.
        """
        if not capture_dates:
            return

        try:
            self._repository.update_capture_date_many(
                {candidate.image.id: capture for candidate, capture in capture_dates}
            )
        except Exception:
            for candidate, capture in capture_dates:
                try:
                    self._repository.update_capture_date(candidate.image.id, capture)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                summary.capture_dates_written += 1
            return

        summary.capture_dates_written += len(capture_dates)

    def _write_positions(
        self,
        positions: list[tuple[IndexCandidate, PositionReading]],
        summary: IndexingSummary,
    ) -> None:
        """Posiciona as linhas que esta janela pulou, numa gravação, recuando por linha.

        O gêmeo de `_write_capture_dates()`, para o RFC-032 §8, e alimentado
        pelos mesmos dois ramos pelo mesmo motivo: esquecer `REFRESH_METADATA`
        deixaria todo arquivo já copiado entre discos sem posição para sempre
        -- e copiar entre discos é a operação central da coleção a que este RFC
        serve (RFC-027 §2.3). `EMBED` não passa por aqui; `save_indexed_many()`
        grava a linha inteira a partir da entidade, posição incluída.

        Uma gravação separada, e não uma combinada com as datas, porque as duas
        decisões são separadas: uma linha pode precisar de posição e não de
        data, e juntá-las num só comando gravaria um valor que ninguém decidiu
        gravar ou precisaria de um terceiro caminho de código para evitar isso.
        """
        if not positions:
            return

        try:
            self._repository.update_position_many(
                {candidate.image.id: reading for candidate, reading in positions}
            )
        except Exception:
            for candidate, reading in positions:
                try:
                    self._repository.update_position(candidate.image.id, reading)
                except Exception as exc:
                    summary.failures.append(
                        IndexingFailure(
                            path=str(candidate.image.display_path), error=exc
                        )
                    )
                    continue
                summary.positions_written += 1
            return

        summary.positions_written += len(positions)


def _timed(
    candidates: Iterable[IndexCandidate], summary: IndexingSummary
) -> Iterator[IndexCandidate]:
    """Produz `candidates`, somando a espera de cada um a `candidate_wait_seconds`.

    O tempo gasto dentro de `next()` e nada mais: o que o consumidor faz com
    um candidato acontece entre duas esperas e não é contado.
    """
    iterator = iter(candidates)
    while True:
        started = time.perf_counter()
        try:
            candidate = next(iterator)
        except StopIteration:
            summary.candidate_wait_seconds += time.perf_counter() - started
            return
        summary.candidate_wait_seconds += time.perf_counter() - started
        yield candidate


def _bytes_of(candidate: IndexCandidate) -> bytes | None:
    """Os bytes que a leitura antecipada leu deste arquivo, ou `None` para lê-lo."""
    return candidate.content.data if candidate.content is not None else None


def _release(candidates: Iterable[IndexCandidate]) -> None:
    """Devolve os bytes dos arquivos de que o pipeline não precisa mais (RFC-045 §5.5).

    Chamado em todo caminho pelo qual um arquivo sai do pipeline: pulado,
    atualizado, com falha no hash, gravado num lote, ou deixado numa janela
    que uma parada abandonou. Liberar duas vezes não faz mal, então nenhum
    caminho precisa saber se outro chegou antes.
    """
    for candidate in candidates:
        if candidate.content is not None:
            candidate.content.release()


def windowed(
    candidates: Iterable[IndexCandidate], size: int
) -> Iterator[Sequence[IndexCandidate]]:
    """Produz janelas consecutivas de tamanho fixo sem materializar a fonte.

    `FilesystemImageProvider.discover()` ordena a sua saída, e o RFC-024 §10
    conta com isso: entrada idêntica produz janelas idênticas, lotes idênticos
    e, portanto, uma execução reproduzível.

    Pública desde o RFC-028, porque o backfill de data de captura percorre os
    candidatos nas mesmas janelas de leitura antecipada e não deveria carregar
    uma segunda cópia.
    """
    iterator = iter(candidates)
    while window := list(islice(iterator, size)):
        yield window
