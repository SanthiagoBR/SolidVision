"""De onde veio a posição de uma imagem (RFC-032 §4.4).

O mesmo desenho de três estados de `CaptureSource` (RFC-028 §4.2), aplicado
ao segundo fato que o scan lê do mesmo cabeçalho. Com ele, "que fração da
coleção tem posição?" é um `GROUP BY`, e não uma releitura de cada arquivo,
e um backfill sabe quais linhas vale a pena revisitar.
"""

from __future__ import annotations

import enum


class PositionSource(enum.StrEnum):
    """De onde `latitude`/`longitude` foram lidas, ou que nada foi lido.

    Um `StrEnum` persistido numa coluna `String` simples, e não num `ENUM`
    nativo do PostgreSQL. Dois membros futuros já têm nome e,
    deliberadamente, **ainda não** são membros: `'manual'` -- o usuário
    marca o ponto no mapa -- e `'subject_estimated'` -- o ponto fotografado,
    calculado a partir do gimbal e da altitude (RFC-032 §9 e §10). Cada um
    tem antes um escritor a construir, e acrescentar qualquer dos dois não
    pode custar uma migration.

    **A ausência de valor não é um membro.** Uma linha cuja origem é NULL
    *nunca foi examinada* -- é anterior ao RFC-032, ou foi escaneada com
    `extract_gps` desligado --, enquanto `UNKNOWN` quer dizer que ela *foi*
    examinada e não tem posição utilizável. Os dois não podem se fundir: um
    scan só grava linhas NULL, e é isso que mantém sem escritas o re-scan de
    uma coleção que não mudou; e arquivos sem GPS são muitos justamente na
    coleção a que este RFC serve.

    **Não há membro XMP.** O RFC-032 §2.3 mediu 40 arquivos DJI reais com um
    pacote XMP `drone-dji` e não achou latitude nem longitude em nenhum; um
    membro para uma origem que nunca foi vista guardando uma posição seria
    um fallback que nunca dispara.
    """

    EXIF_GPS = "exif_gps"
    """O IFD de GPS, apontado pela tag 0x8825 no IFD0.

    A posição da *aeronave*, não do que ela fotografou (RFC-032 §2.2). O
    raio da busca absorve a diferença; nada aqui a corrige.
    """

    UNKNOWN = "unknown"
    """O arquivo foi examinado e não tem posição utilizável.

    Vem com `latitude = longitude = NULL`. Uma imagem com esta origem nunca
    casa com um círculo de busca, por maior que seja (RFC-020).
    """

    @property
    def precedence(self) -> int:
        """Posição na cadeia; quanto maior, mais forte a afirmação.

        Existe para a regra `--force` do backfill, como a de
        `CaptureSource`: reexaminar uma linha só pode trocar a origem dela
        por uma pelo menos tão forte, para que um arquivo que hoje não abre
        não transforme um `exif_gps` guardado em `unknown` (RFC-032 §8.1).
        Separada da cadeia da data de captura de propósito -- um arquivo
        pode perder a data e manter a posição, e cada fato é julgado por si.
        """
        return _PRECEDENCE[self]


_PRECEDENCE = {
    PositionSource.EXIF_GPS: 1,
    PositionSource.UNKNOWN: 0,
}
