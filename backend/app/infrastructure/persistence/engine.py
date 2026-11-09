"""Configuração do engine SQLAlchemy da infraestrutura de persistência.

Duas origens para a conexão (RFC-034 §7.1), e uma função que constrói o engine
para qualquer das duas:

`external` -- as cinco configurações `DATABASE_*`: o `.env` e o container
Docker. A URL está inteira quando o engine é construído.

`embedded` -- o cluster do usuário. A porta dele é escolhida a cada início do
servidor e pode mudar sob uma API em execução (RFC-033 §8.2), então o engine é
construído **sem porta e sem senha**, e um listener de `do_connect` pede as duas
ao adaptador a cada nova conexão física. É isso que permite importar este
módulo com o cluster parado, deixa a porta mudar e mantém a senha fora de
`Settings` e fora do ambiente de todo processo. Um cluster que não está lá é um
erro de conexão que diz isso, e nunca uma tentativa em `localhost:5432` (§7.4).
O ambiente não chega a uma conexão dessas: os parâmetros de libpq que o
adaptador dá às suas próprias conexões, e nenhuma variável `PG*` deixada no
processo (§7.5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import Pool

from app.infrastructure.config.settings import DatabaseSource, Settings, settings
from app.infrastructure.database.embedded.cluster import EmbeddedPostgres
from app.infrastructure.database.embedded.cluster_home import ClusterHome
from app.infrastructure.database.embedded.database_setup import (
    CONNECT_TIMEOUT_SECONDS,
    DATABASE,
    LIBPQ_OVERRIDES,
    LOOPBACK,
    SUPERUSER,
    ClusterConnection,
    discard_libpq_environment,
)
from app.infrastructure.database.embedded.errors import EmbeddedPostgresError
from app.infrastructure.database.embedded.runtime import PostgresRuntime
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

DRIVER = "postgresql+psycopg"

Locate = Callable[[], ClusterConnection]


def build_engine(
    configuration: Settings = settings,
    *,
    poolclass: type[Pool] | None = None,
    locate: Locate | None = None,
) -> Engine:
    """O engine da origem de banco de `configuration`.

    `connect_timeout` vai para as duas: sem ele, uma conexão a uma porta em que
    ninguém escuta ainda estava sendo tentada depois de 25 s, e foi esse o tempo
    que o `/health` levou para responder 503 (RFC-034 §2.3).

    `locate` substitui o próprio `locate()` do adaptador; é para os testes.
    """
    options: dict[str, Any] = {
        "echo": configuration.debug,
        "future": True,
        "pool_pre_ping": True,
    }
    if poolclass is not None:
        options["poolclass"] = poolclass
    if configuration.database_source is DatabaseSource.EMBEDDED:
        return _embedded_engine(locate or _cluster_locator(configuration), options)
    return create_engine(
        # `URL.create` escapa a senha; interpolá-la numa string, como
        # `Settings.database_url` fazia, não escapava (RFC-033 §7.2).
        URL.create(
            DRIVER,
            username=configuration.database_user,
            password=configuration.database_password,
            host=configuration.database_host,
            port=configuration.database_port,
            database=configuration.database_name,
        ),
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        **options,
    )


def _embedded_engine(locate: Locate, options: dict[str, Any]) -> Engine:
    engine = create_engine(
        URL.create(DRIVER, username=SUPERUSER, host=LOOPBACK, database=DATABASE),
        # Os parâmetros que o adaptador dá às suas próprias conexões, da mesma
        # constante: a libpq só lê `PGOPTIONS`, `PGSSLMODE` e `PGGSSENCMODE`
        # para um parâmetro que não recebeu (§7.5).
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS, **LIBPQ_OVERRIDES},
        **options,
    )

    @event.listens_for(engine, "do_connect")
    def supply_the_cluster(
        dialect: Any, connection_record: Any, cargs: Any, cparams: dict[str, Any]
    ) -> None:
        try:
            found = locate()
        except EmbeddedPostgresError as error:
            # A exceção que o SQLAlchemy embrulha no seu próprio
            # `OperationalError`, ao qual o `/health` já responde com 503
            # (RFC-008).
            raise psycopg.OperationalError(str(error)) from None
        # As variáveis que nenhum parâmetro cobre (`PGTZ`, `PGSERVICE`...),
        # tiradas do processo logo antes de a libpq lê-las (RFC-034 §7.5).
        discard_libpq_environment()
        cparams.update(
            host=found.host,
            port=found.port,
            user=found.user,
            password=found.password,
            dbname=found.database,
        )

    return engine


def _cluster_locator(configuration: Settings) -> Locate:
    """`locate()` sobre o cluster do usuário, construído na primeira conexão.

    Não quando o engine é construído: importar este módulo não pode exigir que o
    runtime esteja lá, muito menos que o cluster esteja de pé (§7.3).
    """
    clusters: list[EmbeddedPostgres] = []

    def locate() -> ClusterConnection:
        if not clusters:
            clusters.append(
                EmbeddedPostgres(
                    runtime=PostgresRuntime(
                        configuration.embedded_postgres_runtime_directory
                    ),
                    home=ClusterHome(configuration.embedded_postgres_directory),
                )
            )
        return clusters[0].locate()

    return locate


EngineInstance: Engine = build_engine()

logger.info("SQLAlchemy engine created")
