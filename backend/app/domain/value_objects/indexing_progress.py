"""Os quatro contadores que uma indexação informa enquanto ainda está rodando.

Um value object do Domain, e não o `IndexingSummary` da Application, e a
divisão é uma exigência das camadas, e não gosto. A porta de progresso de
que o RFC-029 precisa mora em `app/domain/services/`, ao lado de
`ContentHasherPort`; uma porta cuja assinatura citasse `IndexingSummary`
faria o Domain importar a Application, o que é ao contrário e que nada em
`app/domain/` faz hoje.

A forma também é a melhor de que depender. `IndexingSummary` traz tempos,
registros de recuo e falhas por arquivo porque uma execução terminada é
relatada a uma pessoa; uma linha de job traz quatro inteiros e uma flag
porque um cliente está desenhando uma barra de progresso. Acoplar os dois
arrastaria todo campo futuro do resumo pelo banco.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexingProgress:
    """Até onde uma execução chegou, no momento da pergunta.

    Cada contador só cresce dentro de uma execução, e é isso que deixa um
    cliente consultar e comparar. Eles *não* são comparáveis entre
    retomadas: um job retomado começa a contar do zero e pula tudo antes do
    checkpoint, então `discovered_files` depois de um reinício é menor do que
    era antes dele. O RFC-029 §10 aceita isso -- o checkpoint salva o scan, e
    o problema do denominador honesto é o da §4.12.
    """

    discovered_files: int = 0
    processed_images: int = 0
    """Imagens que ganharam ou renovaram um embedding."""

    skipped_images: int = 0
    """Imagens que a decisão incremental deixou passar sem inferência.

    Informado à parte porque, sem isso, um novo scan de um disco indexado
    parece travado: 39.000 de 40.000 arquivos pulados apareceriam como
    `processed=1000 / discovered=40000` e uma barra que nunca anda (RFC-029
    §5.1).
    """

    failed_images: int = 0

    discovery_complete: bool = False
    """Se o scan terminou, o que faz de `discovered_files` um total.

    Falso durante a maior parte da vida de um job, e o cliente precisa
    mostrar isso como "lendo N arquivos", e não como porcentagem. A
    descoberta é um gerador (RFC-021), então não há denominador até ela se
    esgotar, e contar antes seria percorrer o disco duas vezes (RFC-029
    §4.12 do build prompt).
    """
