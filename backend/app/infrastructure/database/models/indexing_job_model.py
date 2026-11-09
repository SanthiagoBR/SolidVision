"""Modelos SQLAlchemy dos jobs de indexação e de seus escopos (RFC-029).

**Nenhuma das duas tabelas tem chave estrangeira para `images`, e nenhuma pode
ganhar uma.** O RFC-027 §6.2 justificou a própria prioridade observando que
reescrever a identidade das imagens só continua barato enquanto nada referencia
`images.id`, e apontou este RFC como o que poderia fechar essa janela. Não
fecha: um job se refere a um dispositivo e a um conjunto de pastas, seus
contadores são agregados e seu checkpoint é um caminho. Uma tabela de associação
ligando jobs a imagens seriam 40.000 linhas por job, para um log sem leitor
(RFC-029 §11). `test_indexing_job_model.py` lê os metadados do SQLAlchemy e
falha se uma chave dessas aparecer.

**Os timestamps aqui são `TIMESTAMPTZ`, ao lado de um `images.captured_at` que
não é, e os dois estão certos.** Uma data de captura é a leitura do relógio
local da câmera, sem fuso nenhum, e por isso o RFC-028 §5 a guarda sem fuso.
Tudo o que está abaixo é um instante na máquina que roda o job -- quando ele
entrou na fila, quando um worker provou pela última vez que estava vivo --, e
um instante sem fuso é ambíguo. Quem tiver a tentação de uniformizar os dois
deve ler os dois comentários antes.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.entities.indexing_job import IndexingJob, JobStatus
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.indexing_progress import IndexingProgress
from app.domain.value_objects.job_id import JobId
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.persistence.base import Base

ACTIVE_JOB_INDEX = "uq_one_active_job_per_device"
"""O índice único parcial do RFC-029 §9, nomeado uma vez só.

Nomeado como constante do módulo porque o repositório PostgreSQL precisa
reconhecer exatamente esta string num `IntegrityError` para traduzi-lo em
`DeviceBusyError`. Casar pelo texto da mensagem quebraria no dia em que o
PostgreSQL mudasse a redação ou o servidor rodasse em outro locale, e tratar
*qualquer* `IntegrityError` como "dispositivo ocupado" relataria uma chave
estrangeira inválida como um disco ocupado.
"""


class IndexingJobModel(Base):
    """Representação SQLAlchemy de uma entidade de domínio `IndexingJob`."""

    __tablename__ = "indexing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("devices.id", name="fk_indexing_jobs_device_id"),
        nullable=False,
    )
    """De qual disco é este job.

    Um dispositivo, e não o `collection_id` do `ARCHITECTURE.md` §15:
    `Collection` ainda é um marcador de lugar (RFC-027 §10), e um dispositivo
    é o que o sistema de fato tem.
    """

    status: Mapped[str] = mapped_column(String, nullable=False)
    """`pending` / `running` / `completed` / `failed` / `cancelled`.

    Texto simples e não um `ENUM` do PostgreSQL, seguindo o `capture_source`
    do RFC-028: um tipo fechado no banco não compra nada aqui e faria
    acrescentar um estado custar uma migration.
    """

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    discovered_files: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """Uma contagem corrente, e **não** um denominador até `discovery_complete`.

    A descoberta é um gerador (RFC-021), então o total não existe até o scan
    terminar. Contar antes significaria percorrer o disco duas vezes, o que, no
    disco mecânico frio para o qual este produto existe, é o custo dominante --
    então o cliente recebe o par honesto no lugar.
    """

    processed_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    skipped_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """Arquivos que a decisão incremental deixou passar (RFC-029 §5.1).

    Sem esta coluna, um job que pulou 39.000 de 40.000 arquivos relata
    `processed=1000` contra `discovered=40000` e parece travado. Ela é a única
    coisa que torna a barra de progresso honesta.
    """

    failed_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    discovery_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    last_processed_relative_path: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    """O checkpoint, relativo ao ponto de montagem do dispositivo.

    Relativo pela mesma razão de todo caminho deste banco ser relativo desde o
    RFC-027: um checkpoint absoluto nomearia uma letra de unidade, e a letra de
    unidade é atribuída pela ordem de montagem. Comparado como caminho, nunca
    como string -- ver `FilesystemImageProvider`.
    """

    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    last_heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """A evidência de que alguém ainda está trabalhando neste job.

    Escrito por um temporizador, e não a cada lote. Um novo scan de um disco
    indexado passa minutos seguidos sem produzir lote nenhum, e um heartbeat
    preso aos lotes faria o reaper matar justamente a execução mais saudável
    possível (RFC-029 §9.1).
    """

    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    scopes: Mapped[list[IndexingJobScopeModel]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    """As pastas a percorrer; zero linhas significa o dispositivo inteiro.

    `selectin` em vez de carregamento lazy porque toda leitura de um job lê os
    seus escopos -- a API os devolve, e o executor os percorre --, então a
    alternativa é uma consulta a mais por job em toda resposta de listagem.
    """

    __table_args__ = (
        Index(
            ACTIVE_JOB_INDEX,
            "device_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        CheckConstraint("discovered_files >= 0", name="discovered_files_non_negative"),
        CheckConstraint("processed_images >= 0", name="processed_images_non_negative"),
        CheckConstraint("skipped_images >= 0", name="skipped_images_non_negative"),
        CheckConstraint("failed_images >= 0", name="failed_images_non_negative"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )
    """Um job ativo por dispositivo, garantido pelo banco (RFC-029 §9).

    Um índice único parcial, não um simples: espera-se que um dispositivo tenha
    muitos jobs terminados e no máximo um que ainda segura o disco. Duas
    criações concorrentes disputam dentro do PostgreSQL, onde a disputa de fato
    está, e não dentro de um `SELECT` em Python pelo qual as duas passariam.

    `pending` está no predicado de propósito. Um job na fila já reservou o
    disco, e mantém essa reserva quando um reaper o devolve à fila -- é isso
    que impede um job interrompido em 60% de perder o lugar para o que quer
    que tenha sido pedido em seguida.
    """

    @classmethod
    def from_domain(cls, job: IndexingJob) -> IndexingJobModel:
        """Cria um modelo ORM a partir de uma entidade de domínio."""
        model = cls(id=job.id.value)
        model.apply(job)
        return model

    def apply(self, job: IndexingJob) -> None:
        """Copia cada campo mutável de `job` para esta linha.

        Compartilhado pela inserção e pela atualização, para que as duas não
        possam discordar sobre o que é um job. `id` fica de fora de propósito:
        a identidade de um job nunca muda, e uma atualização que a reescrevesse
        criaria uma segunda linha.

        Os escopos só são reescritos quando diferem, porque nunca mudam depois
        da criação, e o `delete-orphan` emitiria, caso contrário, um delete e um
        insert a cada heartbeat.
        """
        self.device_id = job.device_id.value
        self.status = job.status.value
        self.created_at = job.created_at or datetime.datetime.now(tz=datetime.UTC)
        self.started_at = job.started_at
        self.finished_at = job.finished_at
        self.discovered_files = job.progress.discovered_files
        self.processed_images = job.progress.processed_images
        self.skipped_images = job.progress.skipped_images
        self.failed_images = job.progress.failed_images
        self.discovery_complete = job.progress.discovery_complete
        self.last_processed_relative_path = (
            str(job.last_processed_relative_path)
            if job.last_processed_relative_path is not None
            else None
        )
        self.error_message = job.error_message
        self.last_heartbeat_at = job.last_heartbeat_at
        self.cancel_requested = job.cancel_requested
        self.attempts = job.attempts

        wanted = [str(scope) for scope in job.scopes]
        if [scope.relative_path for scope in self.scopes] != wanted:
            self.scopes = [IndexingJobScopeModel(relative_path=path) for path in wanted]

    def to_domain(self) -> IndexingJob:
        """Cria uma entidade de domínio a partir deste modelo ORM."""
        return IndexingJob(
            id=JobId(self.id),
            device_id=DeviceId(self.device_id),
            status=JobStatus(self.status),
            scopes=tuple(JobScope(scope.relative_path) for scope in self.scopes),
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            progress=IndexingProgress(
                discovered_files=self.discovered_files,
                processed_images=self.processed_images,
                skipped_images=self.skipped_images,
                failed_images=self.failed_images,
                discovery_complete=self.discovery_complete,
            ),
            last_processed_relative_path=(
                ImagePath(self.last_processed_relative_path)
                if self.last_processed_relative_path
                else None
            ),
            error_message=self.error_message,
            last_heartbeat_at=self.last_heartbeat_at,
            cancel_requested=self.cancel_requested,
            attempts=self.attempts,
        )


class IndexingJobScopeModel(Base):
    """Uma pasta a que um job se restringe, relativa ao dispositivo (RFC-029 §5.2).

    Uma tabela filha, e não um array ou uma coluna JSON na linha do job. A
    pergunta que de fato se faz aos escopos é *"esta pasta está coberta por um
    job em execução?"*, que aqui é um `WHERE` e, em qualquer das outras formas,
    uma varredura em memória.

    A chave primária é o par, o que torna impossível uma linha de escopo
    duplicada -- uma segunda guarda atrás de `normalize_scopes()`, e barata,
    já que o par é a chave de qualquer busca de qualquer jeito.
    """

    __tablename__ = "indexing_job_scopes"

    job_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey(
            "indexing_jobs.id",
            name="fk_indexing_job_scopes_job_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    """Onde a pasta fica dentro do dispositivo, nunca um caminho absoluto.

    `ON DELETE CASCADE` na chave acima, ao contrário de `images.device_id`, que
    recusa. A assimetria é deliberada: apagar um job deve levar os seus escopos,
    porque um escopo não tem sentido sem o seu job, enquanto uma linha de imagem
    é um embedding que custou tempo real de inferência (RFC-027 §6.3).
    """

    job: Mapped[IndexingJobModel] = relationship(back_populates="scopes")
