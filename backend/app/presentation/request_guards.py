"""As duas verificações na frente de tudo o que a API serve (RFC-037 §5.2).

    regra                                                      recusa
    o nome em `Host`, sem a porta, é `127.0.0.1` ou            400
    `localhost`
    um método que não seja GET, HEAD ou OPTIONS e traga um     403
    `Origin` vem de `http://127.0.0.1` ou de
    `http://localhost`, em qualquer porta; `Origin: null` é
    recusado

Um middleware ASGI, fora do roteamento: `/health`, a interface e um caminho
desconhecido são verificados como qualquer rota.

**O `Host` fecha o DNS rebinding.** Uma página em `evil.example` cujo nome passa
a resolver para `127.0.0.1` faz as suas requisições com
`Host: evil.example:<port>`, e lê as respostas como se fossem da sua própria
origem. Essas são recusadas aqui, antes de qualquer leitura.

**O `Origin` fecha o formulário.** O que uma página faz sem ler a resposta --
um `<form method="post">` direto para `http://127.0.0.1:<port>` -- traz o
`Host` certo. As rotas com corpo JSON já o recusam, porque um formulário não
consegue enviar `application/json`; os dois `POST` sem corpo, `/reveal` e
`/cancel`, pedem ids que uma página não tem como saber. O risco é pequeno, e o
navegador envia `Origin` em todo `POST` entre sites, então verificá-lo é uma
linha aqui. Um `Origin` ausente passa: `curl`, os scripts de medição, o
`TestClient` e a sondagem do launcher não enviam nenhum. A janela envia o seu,
e o proxy do `npm run dev` envia `localhost:5173`, que passa.

O que continua aberto é um processo nesta máquina, que chega ao loopback com o
cabeçalho que quiser. Isso é com o token por sessão, que o RFC-036 §14 deixou
de fora, e este RFC também.

**`testserver`, o default do `TestClient`, não é aceito.** Aceitá-lo seria
aceitar um nome sem domínio, que a rede local resolve (LLMNR, NetBIOS) e que
alguém nela pode responder. A suíte pede `http://127.0.0.1` no lugar.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost"})
"""Os nomes pelos quais esta API responde. Não `::1`: ela só escuta em `127.0.0.1`."""

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Os métodos cujo `Origin` não é verificado: eles não mudam nada."""


@dataclass(frozen=True)
class Refusal:
    status: int
    detail: str


def host_name(host: str) -> str:
    """O nome de um cabeçalho `Host`, sem a porta, em minúsculas.

    `[::1]:8000` sai como `[`, que aqui não é um nome de loopback, e isso está
    certo: nada escuta em IPv6.
    """
    return host.strip().partition(":")[0].lower()


def origin_is_loopback(origin: str) -> bool:
    """`http://127.0.0.1` ou `http://localhost`, em qualquer porta. `null` não."""
    try:
        parts = urlsplit(origin.strip())
        name = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "http" and name in LOOPBACK_NAMES


def refusal_for(method: str, host: str | None, origin: str | None) -> Refusal | None:
    """Se uma requisição é recusada, e como; `None` a deixa passar."""
    if host is None or host_name(host) not in LOOPBACK_NAMES:
        return Refusal(400, "Invalid host header")
    if (
        method.upper() not in READ_METHODS
        and origin is not None
        and not origin_is_loopback(origin)
    ):
        return Refusal(403, "Cross-origin requests that change something are refused")
    return None


class LoopbackRequestGuard:
    """O middleware: `refusal_for` em toda requisição HTTP, antes do roteamento."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # O lifespan. Nenhuma rota desta API é um WebSocket.
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        method = str(scope["method"])
        refusal = refusal_for(method, headers.get("host"), headers.get("origin"))
        if refusal is None:
            await self.app(scope, receive, send)
            return
        # `%r`, para que o conteúdo do cabeçalho, seja qual for, fique numa
        # linha só do log.
        logger.warning(
            "Refused %s %s with %d: Host %r, Origin %r",
            method,
            scope["path"],
            refusal.status,
            headers.get("host"),
            headers.get("origin"),
        )
        response = JSONResponse({"detail": refusal.detail}, status_code=refusal.status)
        await response(scope, receive, send)
