"""De onde veio a data de captura de uma imagem (RFC-028 §4.2).

Sem isto, `captured_at` seria um timestamp cuja confiabilidade varia de
linha para linha e não pode ser observada. Com isto, um chamador distingue
uma data que a câmera escreveu no instante da foto de uma que um scanner
escreveu na digitalização, um backfill futuro sabe quais linhas vale a pena
revisitar, e "que fração da coleção tem data conhecida?" é um `GROUP BY`, e
não uma releitura de todos os arquivos.
"""

from __future__ import annotations

import enum


class CaptureSource(enum.StrEnum):
    """Um elo da cadeia de recuo do RFC-028 §4, ou o fim dela.

    Um `StrEnum` persistido numa coluna `String` simples, e não num `ENUM`
    nativo do PostgreSQL. Acrescentar um membro -- `gps_derived`,
    `filename_parsed`, as fontes que o RFC-028 §11 antecipa -- custaria então
    uma migration sem nenhum benefício que um tipo fechado do banco de fato
    dê aqui.

    **Não há membro `mtime`, e nunca pode haver.** O RFC-028 §4 é uma
    reversão deliberada do rascunho que terminava a cadeia no timestamp do
    sistema de arquivos: o `mtime` é sistematicamente errado para uma coleção
    cuja vida consiste em ser copiada de disco em disco (RFC-028 §2.1), e um
    membro para ele faria essa data errada parecer mais uma fonte legítima.

    **A ausência de valor também não é um membro.** Uma linha cuja fonte é
    `NULL` *nunca foi examinada* -- é anterior ao RFC-028, ou passou por um
    scan com a extração desligada -- enquanto `UNKNOWN` quer dizer que ela
    *foi* examinada e não tem data nenhuma. As duas não podem se fundir: um
    scan relê o primeiro tipo e deixa o segundo em paz, e é isso que mantém
    sem escritas um novo scan de uma coleção inalterada (RFC-028 §4.2).
    """

    EXIF_ORIGINAL = "exif_original"
    """EXIF `DateTimeOriginal` (0x9003): o relógio da câmera na hora da foto."""

    EXIF_DIGITIZED = "exif_digitized"
    """EXIF `DateTimeDigitized` (0x9004): quando a imagem virou digital.

    Numa câmera digital, as duas normalmente são idênticas. Elas diferem numa
    cópia em papel escaneada ou num negativo de filme, em que esta é a data
    do escaneamento -- ainda um fato escrito no arquivo, que é o que a separa
    do `mtime`, mas uma afirmação mais fraca sobre quando a fotografia foi
    tirada.
    """

    UNKNOWN = "unknown"
    """O arquivo foi examinado e não traz data utilizável.

    Faz par com `captured_at = NULL`. Uma imagem com esta fonte nunca casa
    com um filtro de intervalo de datas (RFC-028 §4.1).
    """

    @property
    def precedence(self) -> int:
        """A posição na cadeia de recuo; mais alto é uma afirmação mais forte.

        Existe para uma regra, fácil de errar de forma destrutiva: reexaminar
        uma linha (o `--force` do backfill) só pode trocar uma fonte por outra
        pelo menos tão forte. Um ingênuo "reler tudo e escrever o que voltou"
        sobrescreveria `exif_original` com `unknown` na primeira vez que um
        arquivo não abrisse -- e passaria em todo teste que só relê arquivos
        saudáveis.
        """
        return _PRECEDENCE[self]


_PRECEDENCE = {
    CaptureSource.EXIF_ORIGINAL: 2,
    CaptureSource.EXIF_DIGITIZED: 1,
    CaptureSource.UNKNOWN: 0,
}
