"""O cluster PostgreSQL embarcado: um adaptador dono do próprio ciclo de vida (RFC-033).

Localiza os binários embarcados, cria o cluster na primeira execução, inicia-o
ou adota um que já esteja de pé, escolhe a porta, se recupera de um
desligamento sujo, prepara o banco e o para. Também responde, sem iniciar
nada, onde um cluster de pé pode ser alcançado: `locate()` (RFC-034 §7.2).
Não decide quem o chama -- o launcher do RFC-036.

Não há, de propósito, porta no Domain (RFC-033 §4.1): nenhum caso de uso
inicia um banco. Quem consome é uma composition root -- hoje a CLI em
`__main__`, depois o launcher --, e, pelo mesmo motivo, nenhuma classe aqui
lê `settings`. Elas recebem caminhos nos construtores, e só `__main__` lê os
`Settings`. Os dois caminhos do Alembic vêm do layout, que é a geometria dos
diretórios, e não configuração (RFC-034 §4.1).

Nada neste pacote importa `app.infrastructure.persistence.engine`. A
dependência vai no sentido contrário: com a origem embarcada, o engine pede a
este pacote a porta e a senha a cada nova conexão.
"""

from __future__ import annotations
