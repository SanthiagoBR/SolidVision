"""O banco dentro do cluster: criar, migrar, conferir (RFC-033 §7.4).

Três passos, nesta ordem, todos idempotentes:

1. **O banco.** O `initdb` cria só `postgres`, `template0` e `template1`. O
   `solidvision` é criado a partir de `template0` com o encoding e o locale
   escritos por extenso, para que nada dependa do estado de `template1`.
2. **As migrations.** Os heads em `alembic_version` contra os heads do
   `ScriptDirectory` do projeto. Iguais: nada a fazer. Atrás: `alembic
   upgrade head` neste processo, nesta conexão, entregue em
   `config.attributes["connection"]`. Uma revisão que o código não conhece:
   **recusar** -- um app mais antigo abrindo um banco que um mais novo migrou.
3. **Os fatos.** A versão major, `UTF8`, `C` e `C`, e `vector` na versão do
   manifesto. Uma divergência nomeia o seu campo. Um `vector` mais antigo que
   o do runtime é um aviso: `ALTER EXTENSION vector UPDATE` pertence aos
   upgrades, que estão fora do escopo.

`CREATE EXTENSION vector` não é um passo aqui. A migration `999b801e80f4`
(RFC-018) já o roda, e um segundo lugar teria de ser mantido em sintonia com a
cadeia de migrations (§2.3).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.infrastructure.config.layout import layout
from app.infrastructure.logging.logger import get_logger

from .errors import ClusterRefusedError, ServerNotReadyError, StartupFailedError

logger = get_logger(__name__)

LOOPBACK = "127.0.0.1"
"""O literal, não `localhost`: o servidor escuta só em IPv4, e o literal tira a
resolução de nomes de toda conexão (§4.2)."""

SUPERUSER = "solidvision"
DATABASE = "solidvision"
EXPECTED_ENCODING = "UTF8"
EXPECTED_LOCALE = "C"

# O layout é a geometria dos diretórios, não configuração: tirar dele os dois
# caminhos mantém este pacote livre de `settings`, como pede o RFC-033 §4.1, e
# de uma âncora própria (RFC-034 §4.1).
ALEMBIC_INI = layout.backend_directory / "alembic.ini"
ALEMBIC_SCRIPTS = layout.backend_directory / "alembic"

MIGRATION_LOCK_KEY = 0x5356_3033
"""Chave do `pg_advisory_xact_lock` ("SV03"): dois migrando ao mesmo tempo esperam."""

LIBPQ_OVERRIDES = {"options": "", "sslmode": "disable", "gssencmode": "disable"}
"""Explícitos, para que `PGOPTIONS`, `PGSSLMODE` ou `PGGSSENCMODE` no ambiente
de quem chama não possam mudar uma conexão ao cluster. A libpq recorre ao
ambiente só para um parâmetro que não foi passado de jeito nenhum.

Uma constante para as conexões do próprio adaptador e para o engine do projeto
(RFC-034 §7.5): um parâmetro acrescentado aqui protege os dois."""

LIBPQ_VARIABLE_PREFIX = "PG"

CONNECT_TIMEOUT_SECONDS = 10


def discard_libpq_environment() -> tuple[str, ...]:
    """Tira toda variável `PG*` do ambiente deste processo.

    O `LIBPQ_OVERRIDES` responde a uma variável dando à libpq o parâmetro que
    ela representa, e algumas variáveis não têm parâmetro para dar. `PGTZ`,
    `PGDATESTYLE` e `PGGEQO` são mandadas ao servidor como configurações da
    sessão, e uma inválida faz o servidor recusar a conexão; `PGSERVICE` nomeia
    uma entrada de um arquivo de serviços, e um nome que não está lá é um erro
    antes de qualquer conexão ser tentada. Medido, com toda variável que a
    libpq lê (RFC-034 §16).

    É a regra que `PostgresRuntime.child_environment` já aplica aos processos
    do próprio servidor, aqui aplicada ao processo que conecta. Ao processo
    inteiro, e não em volta de uma conexão: a libpq lê as variáveis enquanto
    conecta, e devolvê-las depois disputaria com toda outra thread que
    estivesse conectando no mesmo momento. Chamada antes de toda conexão ao
    cluster -- a do adaptador e a do engine -- e nunca para a fonte
    `external`, cujas variáveis `PG*` continuam sendo do desenvolvedor.

    Devolve os nomes que removeu: nada, depois da primeira chamada. Eles vão
    para o log pelo nome, nunca pelo valor -- `PGPASSWORD` é um deles.
    """
    removed = tuple(
        sorted(
            name
            for name in os.environ
            if name.upper().startswith(LIBPQ_VARIABLE_PREFIX)
        )
    )
    for name in removed:
        os.environ.pop(name, None)
    if removed:
        logger.warning(
            "Removed %s from this process's environment: libpq would have "
            "applied them to the connection to the embedded cluster",
            ", ".join(removed),
        )
    return removed


@dataclass(frozen=True)
class ClusterConnection:
    """Tudo de que um cliente precisa para alcançar o cluster. A senha fica fora
    do `repr`, e assim fora de toda linha de log e traceback que imprima um."""

    port: int
    password: str = field(repr=False)
    host: str = LOOPBACK
    user: str = SUPERUSER
    database: str = DATABASE

    def url(self, database: str | None = None) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database or self.database,
        )

    def conninfo(self, database: str | None = None) -> str:
        """Uma string de conexão da libpq para `psycopg.connect`, overrides inclusos.

        Ela carrega a senha: monte-a onde é usada, e não a mande para o log.
        """
        return make_conninfo(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=database or self.database,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            **LIBPQ_OVERRIDES,
        )


class HeadComparison(Enum):
    CURRENT = "current"
    BEHIND = "behind"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PreparedDatabase:
    """O que o `prepare()` achou e fez."""

    created_database: bool
    migrated_from: tuple[str, ...] | None
    """Os heads antes do upgrade, ou `None` quando nada rodou."""
    heads: tuple[str, ...]
    warnings: tuple[str, ...]


def compare_heads(
    database_heads: frozenset[str],
    code_heads: frozenset[str],
    known_revisions: frozenset[str],
) -> HeadComparison:
    """§7.4, passo 2. Um banco sem revisão nenhuma está atrás."""
    if database_heads == code_heads:
        return HeadComparison.CURRENT
    if database_heads - known_revisions:
        return HeadComparison.UNKNOWN
    return HeadComparison.BEHIND


NOT_READY_MESSAGES = (
    "the database system is starting up",
    "the database system is in recovery mode",
    "the database system is not yet accepting connections",
    "the database system is not accepting connections",
)
"""O que um servidor vivo responde com `cannot_connect_now` (SQLSTATE 57P03).

Comparado pelo texto porque o psycopg não dá SQLSTATE para uma recusa no
momento da conexão, e o texto é estável pelo motivo que o §8.1 dá: este build
não tem traduções.
"""


def verify_login(connection: ClusterConnection) -> None:
    """Conecta com a senha, ou diz qual de três coisas deu errado.

    Uma senha recusada quer dizer que o par senha-cluster se perdeu, e o
    adaptador recusa em vez de tentar qualquer outra coisa (§8.4). Um servidor
    que diz que *ainda* não aceita conexões é o único caso que vale tentar de
    novo. Qualquer outra falha é o servidor não respondendo.
    """
    discard_libpq_environment()
    try:
        with psycopg.connect(connection.conninfo("postgres")):
            return
    except psycopg.OperationalError as error:
        text = str(error)
        if error.sqlstate == "28P01" or "password authentication failed" in text:
            raise ClusterRefusedError(
                f"The cluster on port {connection.port} rejected the password in "
                "the password file: the pair is lost. Not trying anything else."
            ) from None
        if error.sqlstate == "57P03" or any(m in text for m in NOT_READY_MESSAGES):
            raise ServerNotReadyError(
                f"The cluster on port {connection.port} is not taking connections "
                f"yet: {text.strip().splitlines()[-1]}"
            ) from None
        raise StartupFailedError(
            f"The cluster on port {connection.port} did not accept a connection: "
            f"{str(error).strip()}"
        ) from error


def _engine(connection: ClusterConnection, database: str | None = None) -> Engine:
    discard_libpq_environment()
    return create_engine(
        connection.url(database),
        poolclass=NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS, **LIBPQ_OVERRIDES},
    )


def _alembic_config() -> Any:
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    return config


def ensure_database(connection: ClusterConnection) -> bool:
    """`CREATE DATABASE` quando falta em `pg_database`. `True` quando criou."""
    engine = _engine(connection, "postgres")
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            exists = admin.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": connection.database},
            ).scalar()
            if exists:
                return False
            try:
                admin.execute(
                    text(
                        f'CREATE DATABASE "{connection.database}" TEMPLATE template0 '
                        f"ENCODING '{EXPECTED_ENCODING}' LOCALE '{EXPECTED_LOCALE}'"
                    )
                )
            except DBAPIError as error:
                # Outro chamador o criou entre o SELECT e aqui.
                if getattr(error.orig, "sqlstate", None) == "42P04":
                    return False
                raise
            return True
    finally:
        engine.dispose()


def database_heads(db: Connection) -> frozenset[str]:
    from alembic.runtime.migration import MigrationContext

    return frozenset(MigrationContext.configure(db).get_current_heads())


def code_revisions() -> tuple[frozenset[str], frozenset[str]]:
    """Os heads do código, e toda revisão que o código conhece."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    heads = frozenset(script.get_heads())
    known = frozenset(revision.revision for revision in script.walk_revisions())
    return heads, known


def upgrade(db: Connection) -> None:
    """`alembic upgrade head`, neste processo, nesta conexão."""
    from alembic import command

    config = _alembic_config()
    config.attributes["connection"] = db
    command.upgrade(config, "head")


def check_facts(db: Connection, major: int, pgvector: str) -> tuple[str, ...]:
    """§7.4, passo 3: recusa uma divergência, avisa de um `vector` mais antigo."""
    row = db.execute(
        text(
            "SELECT current_setting('server_version_num')::int / 10000, "
            "pg_encoding_to_char(d.encoding), d.datcollate, d.datctype, "
            "(SELECT extversion FROM pg_extension WHERE extname = 'vector') "
            "FROM pg_database d WHERE d.datname = current_database()"
        )
    ).one()
    found_major, encoding, collate, ctype, vector = row
    expected = {
        "major version": (found_major, major),
        "encoding": (encoding, EXPECTED_ENCODING),
        "datcollate": (collate, EXPECTED_LOCALE),
        "datctype": (ctype, EXPECTED_LOCALE),
    }
    for name, (found, wanted) in expected.items():
        if found != wanted:
            raise ClusterRefusedError(
                f"The cluster's {name} is {found!r}, not {wanted!r}: it is not the "
                "cluster RFC-033a measured."
            )
    if vector is None:
        raise ClusterRefusedError(
            "The vector extension is not installed after the migrations."
        )
    if _version(vector) > _version(pgvector):
        raise ClusterRefusedError(
            f"vector {vector} is installed, newer than this runtime's {pgvector}."
        )
    if _version(vector) < _version(pgvector):
        return (
            f"vector {vector} is installed, older than this runtime's {pgvector}; "
            "ALTER EXTENSION vector UPDATE belongs to upgrades (RFC-033 section 14).",
        )
    return ()


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def prepare_database(
    connection: ClusterConnection,
    *,
    major: int,
    pgvector: str,
    on_migrating: Callable[[], None] | None = None,
) -> PreparedDatabase:
    """Os três passos do §7.4."""
    created = ensure_database(connection)
    engine = _engine(connection)
    try:
        with engine.connect() as db:
            heads, known = code_revisions()
            found = database_heads(db)
            comparison = compare_heads(found, heads, known)
            migrated_from: tuple[str, ...] | None = None
            if comparison is HeadComparison.BEHIND:
                # Conferido de novo sob uma trava: um segundo chamador que chega
                # enquanto o primeiro migra espera aqui, e então o acha em dia.
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                )
                found = database_heads(db)
                comparison = compare_heads(found, heads, known)
            if comparison is HeadComparison.UNKNOWN:
                db.rollback()
                raise ClusterRefusedError(
                    f"The database is at revision {', '.join(sorted(found))}, which "
                    "this code does not know: a newer SolidVision migrated it. "
                    "Running this code against a schema it does not know is worse "
                    "than not starting."
                )
            if comparison is HeadComparison.BEHIND:
                if on_migrating is not None:
                    on_migrating()
                migrated_from = tuple(sorted(found))
                upgrade(db)
            warnings = check_facts(db, major, pgvector)
            db.commit()
    finally:
        engine.dispose()
    return PreparedDatabase(
        created_database=created,
        migrated_from=migrated_from,
        heads=tuple(sorted(heads)),
        warnings=warnings,
    )
