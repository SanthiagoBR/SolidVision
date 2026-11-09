"""Como a indexação de uma pasta parece vista de fora (RFC-031 §8.2).

Cinco valores, derivados por requisição do histórico de jobs e da contagem de
indexadas. Nada aqui é gravado: uma pasta não é uma linha, e o estado é uma
leitura de `indexing_jobs` e `images` feita no momento em que alguém
perguntou.

**Não há porcentagem, e não há lugar para uma.** RFC-031 §2.3: uma
porcentagem por pasta precisaria de um denominador -- quantos arquivos
suportados há no disco abaixo desta pasta -- e obtê-lo quer dizer percorrer
a subárvore, que é a leitura que o RFC-029 §7.2 mediu como dominante num
disco mecânico a frio. `discovered_files` é um contador crescente
exatamente por essa razão, e não é um total. Então a API informa
`indexed_images`, que é um fato exato da nossa própria tabela, ao lado de
uma destas cinco palavras. *"1.204 indexadas"* é verdade; *"30% indexada"*
não tem do que ser 30%.

O enum mora no Domain, embora a derivação more na Application, e a divisão é
deliberada: *quais estados existem* -- que `partial` é algo que uma pasta
pode ser, e que não é uma falha -- é uma regra do produto. *Ler a lista de
jobs para escolher um* compõe dois repositórios e um localizador, que é para
o que serve um caso de uso (`AI_Context.md`).
"""

from __future__ import annotations

import enum


class FolderState(enum.StrEnum):
    """Quanto de uma pasta este sistema indexou, numa palavra.

    Um `StrEnum`, para que o membro *seja* a string que vai na rede, como
    `JobStatus`. Ao contrário de `JobStatus`, ele nunca é escrito numa coluna
    -- não há tabela de pastas, e o RFC-031 não acrescenta migration -- então
    a escolha é só sobre o JSON.
    """

    INDEXING = "indexing"
    """Os escopos de um job `running` cobrem esta pasta."""

    QUEUED = "queued"
    """Os escopos de um job `pending` cobrem esta pasta; nada se move ainda."""

    PARTIAL = "partial"
    """Parte dela foi indexada, e nada afirma que ela toda foi.

    Três situações caem aqui, e elas são uma situação só vista de três
    jeitos -- *temos linhas abaixo desta pasta e nenhum job concluído que a
    cubra inteira*:

    * o job mais recente que a cobre terminou `cancelled` ou `failed`. O
      `last_processed_relative_path` dele vai na resposta, porque *onde ele
      parou* é aquilo sobre o que um usuário pode agir;
    * um job terminou, mas o escopo dele estava **dentro** desta pasta, e
      não sobre ela -- indexar `2018/janeiro/casamento` deixa `2018/janeiro`
      parcialmente feita, que é literalmente o que esta palavra diz;
    * existem imagens abaixo da pasta e nenhum job do histórico a cobre.
      É um disco indexado antes de os jobs existirem, ou um cujo histórico
      foi aparado. `never_indexed` seria uma afirmação falsa sobre linhas que
      comprovadamente estão lá.
    """

    INDEXED = "indexed"
    """O job mais recente que cobre a pasta inteira terminou `completed`."""

    NEVER_INDEXED = "never_indexed"
    """Nenhum job jamais a cobriu **e** ela não tem imagens indexadas.

    As duas metades são obrigatórias. Uma pasta com linhas e sem job é
    `partial`, e não isto: dizer "nunca indexada" sobre imagens que a busca já
    consegue devolver é o tipo de afirmação confiante e errada que o RFC-031
    §4.2 existe para manter fora da API.
    """
