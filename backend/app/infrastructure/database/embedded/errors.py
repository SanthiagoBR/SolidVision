"""O que o adaptador do cluster embarcado levanta (RFC-033).

Falhas de infraestrutura, não `DomainError`s: o domínio não tem opinião sobre
o `pg_ctl`. Toda mensagem diz o que foi observado e, quando há, o que fazer --
elas chegam à tela de um fotógrafo pelo launcher do RFC-036, e uma recusa que
não diz por quê é um chamado de suporte.
"""

from __future__ import annotations


class EmbeddedPostgresError(RuntimeError):
    """O cluster embarcado não pôde ser levado ao estado pedido."""


class RuntimeInvalidError(EmbeddedPostgresError):
    """O diretório do runtime não é um que o `scripts/vendor_postgres.py` gerou.

    Levantado tanto por um manifesto ausente quanto por um binário ausente:
    apontar o adaptador para uma instalação qualquer do PostgreSQL -- a da
    EnterpriseDB em `Program Files`, por exemplo -- falha aqui,
    explicitamente, em vez de funcionar com outra versão (RFC-033 §5.2).
    """


class ClusterRefusedError(EmbeddedPostgresError):
    """O cluster existe, e o adaptador não vai tocar nele.

    Outra versão major, um cluster preso a outro runtime, uma senha que o
    cluster rejeita, um schema mais novo que o código, fatos que não são os
    que o RFC-033a mediu. Em todos os casos a ação segura é não fazer nada, e
    a mensagem diz qual deles foi.
    """


class ClusterNotRunningError(EmbeddedPostgresError):
    """O `locate()` não achou servidor pronto para conexão (RFC-034 §7.2).

    O `locate()` só lê: não sobe nada, então qualquer coisa que não seja um
    cluster pronto é este erro, dizendo o que foi achado no lugar e quem o
    sobe. O engine o transforma num erro de conexão (§7.3), e ele nunca é
    respondido tentando `DATABASE_*` no lugar (§7.4).
    """


class StartupFailedError(EmbeddedPostgresError):
    """O servidor não subiu, por um motivo que tentar de novo não muda.

    Carrega o que o servidor registrou no log, que é onde ele diz por quê
    (RFC-033 §8.1).
    """


class ServerNotReadyError(StartupFailedError):
    """O servidor está de pé e diz que ainda não aceita conexões.

    A única recusa que esperar muda. O `postmaster.pid` é reescrito quando o
    servidor sobe e quando para, mas **não** quando ele se reinicializa
    depois que um dos seus próprios processos morre: nesses segundos o
    arquivo ainda diz `ready`, e só uma tentativa de conexão ouve "the
    database system is in recovery mode" (medido, RFC-033 §18).
    """


class StopFailedError(EmbeddedPostgresError):
    """Nem o desligamento `fast` nem o `immediate` pararam o servidor.

    O adaptador nunca mata o postmaster (RFC-033 §8.5): decidir se isso vale
    os órfãos que deixa é escolha do launcher.
    """
