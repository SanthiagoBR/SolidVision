"""As duas guardas na frente de tudo o que age na máquina do usuário (RFC-030).

`POST /api/v1/images/{id}/reveal` inicia um processo no computador em que a
API roda. O RFC-030 §2.1 derrubou a objeção do RFC-026 a publicar caminhos
de arquivo *porque a implantação é local*, e uma condição em que se apoia uma
decisão de segurança precisa ser verificada quando a requisição chega, não
suposta quando o código é lido. Daí duas guardas, independentes de
propósito:

* **Guarda 1 -- configuração.** `settings.allow_local_file_actions`,
  `False` a menos que o operador a ligue. A intenção do operador.
* **Guarda 2 -- loopback.** O endereço de quem chama precisa ser de
  loopback, diga o que disser a configuração. Um fato sobre quem está
  chamando, que continua valendo para um operador que subiu a API em
  `0.0.0.0` sem perceber -- justamente a implantação em que a objeção do
  RFC-026 volta.

Nenhuma substitui a outra. A configuração sozinha não protege uma API
exposta na rede; a verificação de loopback sozinha é satisfeita por
qualquer coisa que faça um túnel até localhost, e não diz que o operador
queria isso.

As duas são dependências do FastAPI declaradas no decorator da rota, que o
FastAPI resolve antes dos parâmetros do próprio endpoint. Uma requisição
recusada, portanto, nunca constrói o caso de uso, nunca abre uma sessão no
banco e nunca enumera um volume.
"""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request, status

from app.infrastructure.config.settings import settings


def require_local_file_actions_enabled() -> None:
    """Guarda 1: responde exatamente como responderia uma rota que não existe.

    404 com o próprio `{"detail": "Not Found"}` do FastAPI, não 403. RFC-030
    §6: com a configuração desligada, a rota não existe, em vez de existir e
    recusar -- um cliente sondando a API não aprende com ela nada que um
    caminho desconhecido não lhe diria.
    """
    if not settings.allow_local_file_actions:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def require_loopback_client(request: Request) -> None:
    """Guarda 2: recusa quem chama de um endereço que não é de loopback.

    403, porque a esta altura a rota existe -- o operador a ligou -- e o
    problema é quem chama. O endereço é o que o servidor ASGI informa para
    a conexão. Um proxy reverso na mesma máquina faz todo chamador parecer
    local, a menos que o servidor seja instruído a confiar nos cabeçalhos de
    encaminhamento dele, e esse é um dos motivos de a guarda 1 existir.

    Uma requisição sem nenhum endereço de cliente é recusada:
    "desconhecido" não é "local".
    """
    client = request.client
    if client is None or not is_loopback_address(client.host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Local file actions are only available from this machine.",
        )


def is_loopback_address(host: str) -> bool:
    """Se `host` é um endereço IP literal de loopback, IPv4 ou IPv6.

    Só endereços literais contam. `localhost` é um nome, e um nome é o que
    um resolvedor disser que é; servidores ASGI informam o endereço do par,
    então um nome aqui quer dizer que algo no caminho o reescreveu.

    Um endereço IPv6 mapeado de IPv4 -- `::ffff:127.0.0.1`, que um socket
    dual-stack informa para um chamador de loopback IPv4 -- é julgado pelo
    endereço IPv4 dentro dele. `ipaddress` não faz isso no `is_loopback` em
    toda versão do Python em que isto poderia rodar, e errar aqui recusaria
    um chamador local legítimo.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback
