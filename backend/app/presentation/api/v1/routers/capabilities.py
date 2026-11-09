"""O que esta instalação sabe fazer, atrás da guarda de loopback (RFC-031 §9).

A UI precisa saber se desenha o *"abrir no Explorer"*. Hoje não consegue: com
`allow_local_file_actions` desligado, `POST /images/{id}/reveal` responde
**404 com o corpo de uma rota que não existe** (RFC-030 §6), então descobrir a
resposta tentando significa um botão que some depois que o usuário clicou nele.

**Uma guarda, e a assimetria é o desenho.** Esta rota leva
`require_loopback_client` e *não* `require_local_file_actions_enabled`, que é
o tipo de inconsistência que alguém arruma acrescentando a segunda. Acrescentá-la
quebraria a rota: com a configuração desligada, a rota sumiria -- e "desligado"
é justamente a resposta que a UI veio buscar aqui.

Só a guarda de loopback é o que mantém o 404 honesto. A razão dela é que *um
cliente sondando a API não aprende nada que um caminho errado não lhe diria*;
uma rota anunciando a configuração a qualquer um desfaria metade disso. Então
quem está nesta máquina -- a UI local, o único cliente legítimo -- recebe a
resposta, e qualquer outro recebe um 403 e não aprende nada sobre como este
servidor está configurado.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Depends, status

from app.application.use_cases.search_images import MAX_SEARCH_LIMIT
from app.infrastructure.config.settings import settings
from app.presentation.local_file_actions import require_loopback_client
from app.presentation.schemas.capabilities_schema import CapabilitiesSchema

router = APIRouter(tags=["capabilities"])


@router.get(
    "/capabilities",
    status_code=status.HTTP_200_OK,
    response_model=CapabilitiesSchema,
    summary="What this installation supports",
    dependencies=[Depends(require_loopback_client)],
    responses={
        403: {
            "description": (
                "The caller is not on this machine. The body carries no "
                "configuration at all."
            )
        }
    },
)
def get_capabilities() -> CapabilitiesSchema:
    """Informa as seis coisas por causa das quais um cliente desenha diferente.

    `local_file_actions` espelha `settings.allow_local_file_actions`
    exatamente, inclusive quando é `False` -- um `200` dizendo "não" é a razão
    inteira de esta rota existir, e é por isso que a guarda de configuração
    não está nela.

    `platform` é `sys.platform`, lido aqui em vez de inferido de qualquer
    coisa: só o adaptador de volumes do Windows e o revelador de arquivos do
    Windows são distribuídos, e um cliente tem direito de saber qual sistema
    operacional respondeu (RFC-027 §4.1).

    `version` é `settings.project_version` -- a versão do pacote, que é
    `0.1.0`. O exemplo do RFC-031 §9 mostra `0.5.0`; esse é o número da
    sprint, e o número da sprint não é a versão de nada que é distribuído. A
    configuração é informada como está, em vez de o projeto ser renumerado
    para bater com uma ilustração.

    `max_search_limit` e `min_radius_m` vieram com o RFC-038 (§2.4), e cada um
    passa pelo critério: o tamanho que o *"Mostrar mais"* pede, e o menor raio
    da lista. Os dois são política que a API já impõe -- `MAX_SEARCH_LIMIT` no
    caso de uso, `settings.min_radius_m` do arquivo do usuário -- e uma cópia
    de qualquer um deles na interface é a cópia que o RFC-026 §8.2 se recusou
    a pôr na assinatura da rota: ela divergiria.

    `address_search` veio com o RFC-043: o campo de endereço só é desenhado
    quando o `/geocode` responde.

    O critério para um sétimo campo é *a UI desenha algo diferente por causa
    dele*. Sem um critério isto vira um despejo de configuração, que é o risco
    que o RFC-031 §15 nomeia.
    """
    return CapabilitiesSchema(
        local_file_actions=settings.allow_local_file_actions,
        platform=sys.platform,
        version=settings.project_version,
        max_search_limit=MAX_SEARCH_LIMIT,
        min_radius_m=settings.min_radius_m,
        address_search=settings.allow_address_search,
    )
