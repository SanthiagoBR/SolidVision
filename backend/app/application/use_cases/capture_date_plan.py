"""Se uma data de captura recém-lida deve ser gravada por cima da guardada.

O RFC-028 lê uma data de captura de todo arquivo que um scan descobre,
inclusive dos que a verificação incremental pula. Gravada de volta sem
condição, é um `UPDATE` por arquivo por scan -- 100 000 gravações a cada vez que
uma coleção inalterada é escaneada de novo, destruindo a propriedade que torna
um pulo barato. Esta função é a condição, e é compartilhada pelos dois
chamadores que precisam dela -- o scan de indexação e o backfill --, para que
eles não possam chegar a discordar sobre em quais linhas uma data pode ser
gravada.

Fica separada de `plan_indexing()` de propósito. Aquela função decide se é
preciso *refazer o embedding*, e nada de uma data de captura pode influenciar
isso (RFC-028 §6.1); uma função única devolvendo as duas respostas estaria a
uma comparação descuidada de quebrar isso.
"""

from __future__ import annotations

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource


def capture_date_to_write(
    stored: CaptureSource | None,
    discovered: CaptureDate | None,
    force: bool = False,
) -> CaptureDate | None:
    """Devolve a data de captura a gravar numa linha, ou `None` para não gravar nada.

    `stored` é o `capture_source` atual da linha, como a leitura antecipada de
    metadados o leu; `discovered` é o que o scan acabou de ler do arquivo.

    **Modo normal -- todo scan de indexação, e o backfill por padrão.** Grava
    só quando a linha nunca foi examinada (`stored is None`). Depois do
    primeiro scan que vê um arquivo, um novo scan não grava nada para ele,
    seja o que for que encontre. Isso é seguro, não preguiçoso: se o EXIF
    mudou, os bytes mudaram, o hash de conteúdo mudou, e a linha seguiu o
    caminho `EMBED`, que reescreve a linha inteira de qualquer jeito.

    **O que o modo normal deliberadamente nunca revisita** é uma linha já
    examinada como `unknown` quando a *extração* melhora -- código novo que lê
    uma tag que o código antigo ignorava. Nada mudou no arquivo, então nada
    num scan consegue perceber. Esse é o único caso para o qual `force` existe.

    **Modo force -- só o `--force` do backfill.** Reexamina também as linhas
    já examinadas, e grava o resultado novo quando ele é pelo menos tão forte
    quanto a fonte guardada na cadeia de fallback. Nunca mais fraco: um
    arquivo que não abre hoje, ou uma regressão no leitor, não pode
    transformar um `exif_original` guardado em `unknown`. A implementação
    ingênua -- reler tudo e gravar o que voltou -- faz exatamente isso, e passa
    em todo teste que só relê arquivos saudáveis. Reescrever `unknown` com
    `unknown` é pulado, como o no-op que é.

    `discovered is None` -- o arquivo não foi examinado, porque a extração
    está desligada ou o arquivo não pôde ser lido -- nunca grava, em nenhum
    dos modos. Gravar registraria "examinado" para um arquivo que ninguém leu.
    """
    if discovered is None:
        return None
    if stored is None:
        return discovered
    if not force:
        return None
    if discovered.source.precedence < stored.precedence:
        return None
    if discovered.source is CaptureSource.UNKNOWN:
        return None
    return discovered
