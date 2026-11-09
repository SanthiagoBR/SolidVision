"""Tradução das falhas de domínio em códigos de status HTTP (RFC-026 §8).

Um handler para toda a hierarquia de `DomainError`, registrado no app em
vez de capturado numa rota. Duas propriedades decorrem disso, e as duas
são o objetivo:

* toda rota fica livre de `try/except`, e mantém a forma que o RFC-026
  §4 exige -- chamar o caso de uso, mapear o resultado, devolver;
* todo erro de domínio que um caso de uso futuro levantar já tem um
  código de status, sem que ninguém precise lembrar de acrescentar um.

**O RFC-029 acrescentou dois status sem acrescentar um registro.** O
status vem de onde o erro fica na hierarquia, não de uma tabela de
classes de exceção mantida na Apresentação: `NotFoundError` responde 404,
`ConflictError` responde 409, e todo o resto continua respondendo 400. Um
novo erro de "não encontrado" herda seu status; não precisa ser
registrado em lugar nenhum, e ninguém pode esquecer de registrá-lo. O
RFC-030 acrescentou uma terceira base do mesmo jeito: `GoneError`
responde 410.

    situação                           erro                          status
    ---------------------------------- ----------------------------- ------
    id de job não aponta para nada     JobNotFoundError                 404
    id de disp. não aponta para nada   DeviceNotFoundError              404
    id de imagem não aponta para nada  ImageNotFoundError               404
    imagem conhecida, nunca indexada   ImageNotIndexedError             409
    imagem sem thumbnail para servir   ThumbnailNotFoundError           404
    dispositivo já tem um job ativo    DeviceBusyError                  409
    cancelar um job já terminado       IllegalJobTransitionError        409
    dispositivo não está plugado       DeviceNotConnectedError          409
    disco plugado, arquivo fora dele   FileGoneError                    410
    escopo absoluto / com .. / sumiu   InvalidJobScopeError             400

As duas últimas linhas valem ser lidas juntas. O RFC-029 §7.1 mandava
"dispositivo desconectado" para 400; isso está errado, e a correção está
registrada no RFC. 400 diz *a requisição precisa ser corrigida*, e nada
nessa requisição precisa -- os mesmos bytes funcionam assim que o disco é
plugado. Um escopo inválido é o oposto: não passaria a funcionar por nada
que acontecesse no mundo.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    ConflictError,
    DomainError,
    GoneError,
    NotFoundError,
)
from app.infrastructure.ai.model_runtime import ModelUnavailableError
from app.infrastructure.geocoding.nominatim_geocoder import (
    AddressSearchUnavailableError,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

STATUS_BY_BASE: tuple[tuple[type[DomainError], int], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ConflictError, status.HTTP_409_CONFLICT),
    (GoneError, status.HTTP_410_GONE),
)
"""As bases que carregam um status, e nada mais.

Curta de propósito, e precisa continuar assim: uma entrada por classe de
exceção seria o registro que este desenho existe para evitar, e se
desalinharia na primeira vez que alguém acrescentasse um erro sem tocar
neste arquivo.
"""

DEFAULT_STATUS = status.HTTP_400_BAD_REQUEST
"""O que um `DomainError` puro ainda quer dizer: entendido, e recusado.

400, não 422, e a diferença é deliberada. O FastAPI já devolve 422 para uma
requisição que nem casa com o endpoint -- um `q` ausente, um `limit` que não
é inteiro --, e isso quer dizer outra coisa: *esta não é uma requisição bem
formada*. Um `DomainError` quer dizer que a requisição foi entendida e a
aplicação a recusou. Achatar os dois apagaria essa distinção e poria a
Apresentação no papel de redescrever falhas que a camada de Aplicação nunca
viu.
"""


def status_for(error: Exception) -> int:
    """Devolve o status da base correspondente mais específica, ou 400.

    "Mais específica" é resolvida pelo próprio MRO da exceção, e não pela
    ordem da tabela acima, então a resposta não depende de como a tupla
    calhou de ser escrita. Com três bases disjuntas, hoje a resposta é a
    mesma de um jeito ou de outro; deixa de ser a mesma no momento em que
    uma terceira base for acrescentada sob uma dessas duas, que é
    exatamente quando um bug sutil de mapeamento seria mais difícil de ver.
    """
    mro = type(error).__mro__
    matches = [
        (mro.index(base), code)
        for base, code in STATUS_BY_BASE
        if isinstance(error, base)
    ]
    if not matches:
        return DEFAULT_STATUS
    return min(matches)[1]


def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Responde a uma requisição entendida, mas recusada, com status e mensagem.

    O parâmetro é tipado como `Exception`, e não `DomainError`, porque essa
    é a assinatura que o registro de handlers do Starlette declara;
    `add_exception_handler`, abaixo, é o que o estreita para a subclasse
    que este handler de fato recebe.

    A mensagem é a do próprio erro de domínio. Essas mensagens são escritas
    para quem fez a requisição ler -- "Device 'HD2' is not connected.
    Plug it in and try again." -- e reescrevê-las aqui seria a
    Apresentação descrevendo falhas que nunca viu.
    """
    code = status_for(exc)
    logger.info("Domain error refused a request: %s -> %d", type(exc).__name__, code)
    return JSONResponse(status_code=code, content={"detail": str(exc)})


def model_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Responde a uma requisição que precisa de um modelo impossível de carregar.

    503, e não 500, pelo mesmo motivo de `/health` responder 503 quando o
    banco está fora (RFC-034): o serviço está indisponível, não quebrado
    (RFC-035 §6.5). Só as rotas que carregam um modelo chegam aqui -- a
    busca, hoje --, então dispositivos, pastas, o mapa e as thumbnails
    continuam respondendo.

    Não é um `DomainError`, e por isso tem handler próprio: o domínio não
    tem opinião sobre um hash. O `detail` é a única linha do erro, que diz
    o modelo, o arquivo e o que fazer, e não traz conteúdo de arquivo.
    """
    logger.warning("A request needed a model that cannot be loaded: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


def address_search_unavailable_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """503, como para um modelo: o endereço pode ser achado um minuto depois (RFC-043).

    O adaptador já registrou o motivo no log, e a mensagem dele nunca traz o
    endereço.
    """
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Liga a `app` o mapeamento dos erros de domínio, e os dois 503."""
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(ModelUnavailableError, model_unavailable_handler)
    app.add_exception_handler(
        AddressSearchUnavailableError, address_search_unavailable_handler
    )
