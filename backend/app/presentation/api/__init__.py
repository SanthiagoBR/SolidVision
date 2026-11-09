"""Pacote de apresentação da API: a aplicação FastAPI e a sua montagem."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.infrastructure.ai.model_runtime import ModelUnavailableError
from app.infrastructure.config.settings import settings
from app.infrastructure.logging.logger import get_logger
from app.presentation.api.v1 import api_v1_router
from app.presentation.error_handlers import register_error_handlers
from app.presentation.frontend import serve_frontend
from app.presentation.model_warm_up import SearchReadiness, SearchState, model_warm_up
from app.presentation.query_image_upload import (
    empty_query_image_directory,
    get_query_image_directory,
)
from app.presentation.request_guards import LoopbackRequestGuard
from app.presentation.routes import health_router

logger = get_logger(__name__)

# O texto codificado no início quando o aquecimento está ligado, e o seu conteúdo
# sustenta mais do que parece.
#
# O RFC-025 §12 registrou dois inícios a frio, não um: ~4,95 s para o checkpoint
# do CLIP e mais ~4,20 s para o tradutor Marian na primeira consulta *em
# português*. Um texto de aquecimento em inglês resolveria o primeiro e deixaria
# o segundo, e o primeiro usuário brasileiro de um processo novo ainda esperaria
# quatro segundos. Uma frase em português percorre o caminho inteiro -- detectar,
# traduzir, aplicar o template, codificar -- e carrega os dois.
#
# É uma frase completa pela razão que o RFC-023 §7.1 mediu: o `langdetect`
# precisa de várias palavras para ser confiável, e chama `fazenda` de turco. Um
# texto curto em português seria lido como outra língua, pularia a tradução e
# aqueceria só o CLIP, parecendo ter aquecido os dois.
_WARM_UP_QUERY = "uma fotografia de teste para aquecer o modelo de busca"


def _warm_up_models() -> SearchReadiness:
    """Carrega e exercita o modelo de embedding antes da primeira busca.

    O motivo é a latência (RFC-026 §10): sem isso, a primeira consulta de um
    processo paga ~4,95 s para carregar o checkpoint do CLIP, o que sozinho
    estoura a meta de um segundo, e a primeira consulta em português paga mais
    ~4,20 s pelo tradutor. Os dois são carregados aqui, e é para isso que
    `_WARM_UP_QUERY` é uma frase em português.

    **Roda numa thread, ao lado da porta aberta** (RFC-036 §5.3). Antes rodava
    antes de o uvicorn abrir a porta, o que também afastava uma corrida: o
    endpoint de busca é um `def`, duas primeiras requisições rodam em duas
    threads, e as duas podiam achar o adaptador não carregado. Os adaptadores
    agora carregam uma vez sob um lock, e uma busca que chega durante o
    aquecimento espera a carga em andamento. Esperar é o custo declarado; o
    ganho é toda rota que não precisa de modelo responder no tempo de um import
    sem `torch`, em vez de depois dele e das duas cargas.

    Os modelos são verificados primeiro quanto a presença e tamanho, o que é
    barato e acha uma instalação quebrada agora, e não na primeira busca
    (RFC-035 §6.1); a carga depois verifica os hashes.

    Uma falha vai para o log e é informada, nunca levantada: a API fica no ar.
    Recusar-se a iniciar derrubaria o `/health` junto com a busca -- a sonda que
    um operador usaria para diagnosticar justamente isso -- e esconderia os
    dispositivos, as pastas, o mapa e as thumbnails, nenhum dos quais precisa de
    modelo (RFC-035 §6.5). O caminho preguiçoso continua: toda busca tenta a
    carga de novo, e responde 503 com a mesma linha enquanto ainda não conseguir.
    """
    from app.infrastructure.ai.model_runtime import default_runtime
    from app.presentation.dependencies import get_embedding_model

    logger.info("Warming up the embedding model")
    started_at = time.perf_counter()
    try:
        default_runtime().check_presence()
        get_embedding_model().encode_text(_WARM_UP_QUERY)
    except ModelUnavailableError as error:
        logger.error("Model warm-up failed, and search answers 503: %s", error)
        return SearchReadiness(SearchState.UNAVAILABLE, str(error))
    except Exception:
        logger.warning(
            "Model warm-up failed; the first query will retry", exc_info=True
        )
        return SearchReadiness(SearchState.ON_DEMAND)
    elapsed = time.perf_counter() - started_at
    logger.info("Embedding model warmed up in %.2fs", elapsed)
    return SearchReadiness(SearchState.READY)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Roda o trabalho de início e de encerramento em volta da vida servida de `app`.

    `lifespan` em vez de `@app.on_event("startup")`, que o FastAPI atual
    marca como obsoleto; este é o primeiro hook de início do projeto, então
    não há estilo existente a seguir.

    Inicia o aquecimento e retorna: o uvicorn só abre a porta depois que isto
    faz o yield, e o aquecimento importa o `torch` (RFC-036 §5.3). O `/health`
    informa como vai o aquecimento, em `search`.

    O aquecimento é `settings.warm_up_models`, cujo padrão é o do modo: ligado
    no instalado, desligado num checkout (RFC-035 §8). Desligado num checkout
    não é timidez quanto a uma flag de configuração: `TestClient(app)` usado
    como context manager -- o que `tests/presentation/test_health.py` faz --
    roda exatamente esta função, e um aquecimento incondicional começaria a
    carregar e calcular o hash de 924 MB de checkpoints por trás de um simples
    `pytest`, num arquivo de teste que nada tem a ver com busca, para uma rota
    que nunca toca no modelo.

    Antes disso, `cache\\query-images` é esvaziada do que um processo que morreu
    deixou lá (RFC-038 §4.2). A pasta vem por `app.dependency_overrides`, para
    que seja aquela em que a rota de upload escreve, num teste como no app.
    """
    directory = app.dependency_overrides.get(
        get_query_image_directory, get_query_image_directory
    )()
    removed = empty_query_image_directory(directory)
    if removed:
        logger.info("Deleted %d query images a previous run left behind", removed)
    if settings.warm_up_models:
        model_warm_up.start(_warm_up_models)
    else:
        model_warm_up.skip()
        logger.info("Model warm-up disabled; the first search will load the model")
    yield


app = FastAPI(
    title=settings.project_name,
    description="Semantic image search API for SolidVision",
    version=settings.project_version,
    lifespan=lifespan,
)

logger.info("FastAPI application initialized")
register_error_handlers(app)
app.include_router(health_router)
app.include_router(api_v1_router)
# Na frente de tudo, `/health` e a interface inclusive (RFC-037 §5.2).
app.add_middleware(LoopbackRequestGuard)
# Depois dos routers, e fora do roteamento deles: um mount em `/assets`, e o 404
# de uma tela da interface transformado em `index.html` (§5.1).
serve_frontend(app, settings.frontend_directory)

__all__ = ["app"]
