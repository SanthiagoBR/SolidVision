"""Se uma posição recém-lida deve ser escrita sobre a guardada (RFC-032).

O scan lê uma posição para cada arquivo que descobre, inclusive os que a
checagem incremental pula. Escrita de volta sem condição, isso dá um `UPDATE`
por arquivo por scan -- a propriedade que o RFC-028 §6 protegeu para as datas
de captura, perdida de novo para as posições. Esta função é a condição,
compartilhada pelos dois chamadores que precisam dela -- o scan de indexação e
o backfill de EXIF -- para que eles não possam passar a discordar sobre em
quais linhas uma posição pode ser escrita.

**Uma função separada de `capture_date_to_write()`, de propósito** (RFC-032
§8). Os dois fatos saem da mesma leitura do cabeçalho, e uma função única
decidindo os dois estaria a uma linha descuidada de escrever uma posição
porque a data precisava ser escrita, ou de deixar um arquivo que perdeu a data
rebaixar a posição. Cada fato tem a sua cadeia, a sua fonte guardada e a sua
decisão.

E separada de `plan_indexing()`, pela razão que o plano da data de captura é:
aquela função decide se é preciso *refazer o embedding*, e uma posição nunca
pode influenciar isso.
"""

from __future__ import annotations

from app.domain.value_objects.position import PositionReading
from app.domain.value_objects.position_source import PositionSource


def position_to_write(
    stored: PositionSource | None,
    discovered: PositionReading | None,
    force: bool = False,
) -> PositionReading | None:
    """Devolve a posição a escrever numa linha, ou `None` para não escrever nada.

    `stored` é o `position_source` atual da linha, como a leitura antecipada
    dos metadados o leu; `discovered` é o que o scan acabou de ler do arquivo.

    **Modo normal -- todo scan de indexação, e o backfill por padrão.**
    Escreve só quando a linha nunca foi examinada (`stored is None`). Depois
    do primeiro scan que vê um arquivo, um novo scan não escreve nada para ele.
    Seguro, e não preguiçoso: se o bloco de GPS mudou, os bytes mudaram, o hash
    do conteúdo mudou, e a linha tomou o caminho `EMBED`, que reescreve a linha
    inteira a partir da entidade.

    **Modo forçado -- só o `--force` do backfill.** Reexamina também as linhas
    já examinadas, e escreve a nova leitura quando ela é pelo menos tão forte
    quanto a fonte guardada. Nunca mais fraca: um `exif_gps` sobrevive a um
    arquivo que hoje lê `unknown`, porque o disco está falhando ou o leitor
    regrediu (RFC-032 §8.1). Reescrever `unknown` com `unknown` é pulado, como
    a operação nula que é.

    `discovered is None` -- não examinado, porque `extract_gps` está desligado
    ou o arquivo não pôde ser lido -- nunca escreve, em nenhum dos modos.
    Escrevê-lo registraria "examinado" para um arquivo que ninguém leu.
    """
    if discovered is None:
        return None
    if stored is None:
        return discovered
    if not force:
        return None
    if discovered.source.precedence < stored.precedence:
        return None
    if discovered.source is PositionSource.UNKNOWN:
        return None
    return discovered
