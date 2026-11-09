"""Pacote de apresentação da API v1.

A versão um da superfície HTTP do produto. O prefixo existe antes de haver
clientes, e não depois, porque uma resposta de busca é exatamente o formato
que ganha uma URL de thumbnail ou um `total` assim que uma UI precisa de um
(RFC-027, RFC-028), e versionar algo já publicado é uma migração, e não uma
decisão.

`/capabilities` (RFC-031 §9) *faz* parte dela, e vale explicar a diferença
para `/health`, porque os dois se parecem. Uma sonda de saúde diz se o
processo está no ar, a um monitor que não é uma pessoa. `/capabilities` diz o
que esta instalação suporta, à UI, para que ela saiba quais botões existem --
é um recurso da API do produto, plausivelmente mudaria de formato numa `v2`, e
está atrás da guarda de loopback justamente porque não é da conta de mais
ninguém.

`/health` deliberadamente não faz parte dela: uma sonda de saúde é
infraestrutura, não um recurso da API do produto, é o que um orquestrador ou um
load balancer chama num caminho fixado em configuração, e uma `v2` da busca
não significaria uma `v2` de "o banco está acessível". Ele fica sem versão em
`app/presentation/routes/`, que não recebe mais rotas de recurso
(RFC-026 §6.1).
"""

from fastapi import APIRouter

from .routers import (
    capabilities_router,
    devices_router,
    geocoding_router,
    images_router,
    jobs_router,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(images_router)
api_v1_router.include_router(jobs_router)
api_v1_router.include_router(devices_router)
api_v1_router.include_router(capabilities_router)
api_v1_router.include_router(geocoding_router)

__all__ = ["api_v1_router"]
