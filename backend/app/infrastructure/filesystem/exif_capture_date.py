"""Lê quando e onde uma foto foi tirada, de dentro do arquivo (RFC-028, RFC-032).

    DateTimeOriginal   ->  CaptureSource.EXIF_ORIGINAL
    DateTimeDigitized  ->  CaptureSource.EXIF_DIGITIZED
    nada utilizável    ->  CaptureSource.UNKNOWN, sem data

    GPS IFD (0x8825)   ->  PositionSource.EXIF_GPS
    nada utilizável    ->  PositionSource.UNKNOWN, sem posição

**Uma abertura, dois fatos** (RFC-032 §4.2). O scan já abre cada candidato para
ler a sua data, e abri-lo de novo para o GPS dobraria o custo medido -- 0,39 ms
virando ~0,8 ms por arquivo, ~40 s virando ~80 s a cada 100.000 arquivos --
para reler os mesmos bytes do cabeçalho. `read_exif_facts()` abre uma vez e
devolve os dois; `read_capture_date()` continua como o leitor da data.

**Os dois fatos são independentes; nenhum deriva do outro.** Cada um mantém os
três desfechos que o RFC-028 §4.3 estabeleceu -- um valor, `unknown`, ou `None`
para "não examinado" -- *por fato*. Um arquivo que não pode ser lido não é
examinado para nenhum dos dois; um arquivo lido sem GPS e com data é `unknown`
na posição e datado na data; um bloco GPS danificado demais para ser lido deixa
a posição `unknown` e não mexe na data.

**O sistema de arquivos não é consultado, nem o `DateTime` do EXIF.**
`st_mtime` é a data da última cópia, numa coleção que vive de ser copiada entre
discos (RFC-028 §2.1), e a tag 0x0132 `DateTime` é o que o software de edição
carimba ao salvar -- a mesma data de modificação com um nome de EXIF. **E não
há recurso ao XMP para a posição**: o RFC-032 §2.3 achou o pacote XMP
`drone-dji` em todos os 40 arquivos DJI reais que leu, e nenhuma latitude ou
longitude em nenhum deles.

Uma unidade própria, e não linhas dentro do laço da descoberta, porque quase
toda linha dela existe para sobreviver a algo que arquivos reais fazem: uma tag
que mora um IFD ao lado de onde parece morar, um marcador de zeros, preenchimento
com NUL, uma letra de hemisfério que sumiu, um cabeçalho truncado, um arquivo que
desapareceu entre ser listado e ser aberto.

Somente leitura. Nada aqui escreve EXIF, nem qualquer outra coisa, num arquivo
da coleção (RFC-028 §10, RFC-032 §10).
"""

from __future__ import annotations

import datetime
import math
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from app.domain.value_objects.capture_date import CaptureDate
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.position import (
    MAX_LATITUDE,
    MAX_LONGITUDE,
    Position,
    PositionReading,
)
from app.domain.value_objects.position_source import PositionSource

EXIF_IFD_POINTER = 0x8769
"""Tag do IFD0 que aponta para o sub-IFD Exif.

`DateTimeOriginal` e `DateTimeDigitized` moram nesse sub-IFD, não no IFD0.
`Image.getexif()[0x9003]` por isso não devolve nada para quase toda fotografia
real, e um código escrito assim passa contra fixtures feitas à mão que põem a
tag no lugar errado e devolve `unknown` para um rolo de câmera inteiro.
"""

GPS_IFD_POINTER = 0x8825
"""Tag do IFD0 que aponta para o IFD de GPS -- não o sub-IFD Exif, um IFD ao lado.

A mesma armadilha de `EXIF_IFD_POINTER`, na porta ao lado (RFC-032 §4.1): as
coordenadas não estão nem no IFD0 nem no sub-IFD de onde vem a data, e um leitor
que procurasse em qualquer dos dois passaria em todo teste construído do mesmo
jeito errado e devolveria `unknown` para 95% da coleção.
"""

GPS_LATITUDE_REF = 0x0001
GPS_LATITUDE = 0x0002
GPS_LONGITUDE_REF = 0x0003
GPS_LONGITUDE = 0x0004
"""Uma coordenada são **quatro** valores, não dois.

`GPSLatitude` e `GPSLongitude` são triplas de grau/minuto/segundo em racionais
sem sinal; o *sinal* mora em `GPSLatitudeRef` (`N`/`S`) e `GPSLongitudeRef`
(`E`/`W`). Descartar a referência não levanta erro nenhum -- põe uma foto do
Paraná no hemisfério norte.
"""

DATE_TIME_ORIGINAL = 0x9003
DATE_TIME_DIGITIZED = 0x9004

EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
"""O `YYYY:MM:DD HH:MM:SS` do EXIF 2.3 -- dois-pontos também na data, e sem fuso."""

NULL_ISLAND_TOLERANCE_DEGREES = 1e-4
"""Quão perto de `0, 0` conta como o marcador de "sem fix de satélite": ~11 m.

Não igualdade exata, porque um fix parcial escreve zeros com ruído (RFC-032
§4.3). O ponto de que ele protege é real -- o Golfo da Guiné, a "Null Island",
onde erros de coordenada se acumulam em bancos de dados do mundo inteiro --, e
"toda foto sem fix, num só ponto do oceano" é o tipo de resposta que parece
certa até alguém clicar nela.
"""

_CHAIN: tuple[tuple[int, CaptureSource], ...] = (
    (DATE_TIME_ORIGINAL, CaptureSource.EXIF_ORIGINAL),
    (DATE_TIME_DIGITIZED, CaptureSource.EXIF_DIGITIZED),
)
"""A cadeia de recurso, a afirmação mais forte primeiro. Dois elos, de propósito."""

_HEMISPHERE_SIGNS: tuple[Mapping[str, int], Mapping[str, int]] = (
    {"N": 1, "S": -1},
    {"E": 1, "W": -1},
)


@dataclass(frozen=True)
class ExifFacts:
    """Os dois fatos que uma leitura de cabeçalho rende, cada um examinado ou não.

    `None` num campo significa *não examinado* -- desligado, ou o arquivo não
    pôde ser lido --, exatamente como `DiscoveredImageFile` o usa. Um valor
    significa examinado, o que inclui o desfecho `unknown`.
    """

    capture_date: CaptureDate | None
    position: PositionReading | None


def read_exif_facts(
    path: Path, capture_date: bool = True, position: bool = True
) -> ExifFacts:
    """Abre `path` uma vez e lê do seu cabeçalho os fatos pedidos.

    Por fato, três desfechos, e a diferença entre os dois últimos é o que
    importa:

    - um valor com uma origem EXIF;
    - `unknown` quando o arquivo foi lido e não guarda nada utilizável -- EXIF
      nenhum (normal em PNG e BMP, e não registrado como nada), um marcador,
      lixo numa tag, meia coordenada, ou bytes que o Pillow não consegue ler.
      Ler os mesmos bytes de novo dá a mesma resposta, então a linha é marcada
      como examinada e um scan posterior a deixa em paz;
    - `None` quando o arquivo não pôde ser *lido* -- apagado desde que foi
      listado, bloqueado, um disco que sumiu. Isso não é um fato sobre o
      arquivo, e registrá-lo como `unknown` impediria todo scan futuro de olhar
      de novo. `None` deixa a linha não examinada, e o próximo scan tenta de
      novo.

    Um fato que não foi pedido é `None` -- não examinado --, seja o que for que
    o arquivo guarde: desligado significa "ninguém olhou", nunca "não há nada"
    (`Settings.extract_capture_date`, `Settings.extract_gps`).

    **Nunca levanta exceção** por nada que um arquivo em disco possa lhe fazer.
    Quem chama é um scan sobre 100.000 arquivos, e um JPEG corrompido não pode
    encerrá-lo (RFC-028 §13).

    Só o cabeçalho é lido: `Image.open()` é preguiçoso, e nada aqui chama
    `load()`, converte ou redimensiona.
    """
    if not capture_date and not position:
        return ExifFacts(capture_date=None, position=None)

    try:
        handle = path.open("rb")
    except OSError:
        return ExifFacts(capture_date=None, position=None)

    with handle:
        try:
            sub_ifd, gps_ifd = _exif_ifds(handle)
        except OSError as exc:
            # O Pillow relata um arquivo malformado como um `OSError("...")`
            # puro, sem errno; o sistema operacional relata uma leitura que
            # falhou com um. Só o primeiro é uma propriedade dos bytes.
            if exc.errno is not None:
                return ExifFacts(capture_date=None, position=None)
            sub_ifd, gps_ifd = {}, {}
        except Exception:
            # Amplo de propósito, e só em volta das chamadas ao Pillow. Um
            # arquivo corrompido aparece como `UnidentifiedImageError`,
            # `SyntaxError`, `struct.error`, `ValueError`, `KeyError` ou
            # `DecompressionBombError`, conforme onde está o dano, e todos
            # significam a mesma coisa aqui: examinado, nada utilizável.
            sub_ifd, gps_ifd = {}, {}

    return ExifFacts(
        capture_date=_capture_date(sub_ifd) if capture_date else None,
        position=_position(gps_ifd) if position else None,
    )


def read_capture_date(path: Path) -> CaptureDate | None:
    """Devolve a data de captura gravada em `path`, ou `None` se ilegível.

    O leitor da data, mantido como o RFC-028 o entregou e agora uma visão de
    `read_exif_facts()`, para que os dois nunca discordem sobre um arquivo. O
    scan chama `read_exif_facts()` diretamente: chamar este e um leitor de
    posição separadamente abriria cada arquivo duas vezes.
    """
    return read_exif_facts(path, capture_date=True, position=False).capture_date


def _exif_ifds(handle: BinaryIO) -> tuple[Mapping[int, object], Mapping[int, object]]:
    """Abre a imagem preguiçosamente, uma vez, e devolve o sub-IFD Exif e o de GPS.

    **Cada IFD é lido por conta própria**, para que o dano em um não leve o
    outro fato junto: um bloco GPS que o Pillow não consegue ler volta vazio --
    posição `unknown` -- enquanto a data ainda é lida do seu próprio IFD
    (RFC-032 §4.2). Uma falha com errno é do disco, não dos bytes, e é deixada
    passar para que quem chama possa responder "não examinado".

    Os avisos ficam silenciados durante a leitura, porque cada um que o Pillow
    levanta aqui já está refletido no valor devolvido. "Corrupt EXIF data" volta
    como um IFD vazio ou parcial, que vira `unknown`; `DecompressionBombWarning`
    dispara para quadros aéreos grandes que nada aqui vai decodificar.

    Além do dobro do limite de pixels do Pillow, `open()` levanta exceção em vez
    de avisar, e um arquivo assim volta `unknown` para os dois fatos mesmo que
    traga EXIF -- uma limitação declarada para ortomosaicos muito grandes, que o
    `--force` do backfill pode revisitar.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Image.open(handle) as image:
            exif = image.getexif()
            return _ifd(exif, EXIF_IFD_POINTER), _ifd(exif, GPS_IFD_POINTER)


def _ifd(exif: Image.Exif, pointer: int) -> Mapping[int, object]:
    """Um IFD como dict simples, ou vazio se estiver ausente ou não for legível."""
    try:
        return dict(exif.get_ifd(pointer))
    except OSError as exc:
        if exc.errno is not None:
            raise
        return {}
    except Exception:
        return {}


def _capture_date(sub_ifd: Mapping[int, object]) -> CaptureDate:
    """Percorre a cadeia do RFC-028 sobre o sub-IFD Exif; `unknown` no fim dela."""
    for tag, source in _CHAIN:
        captured_at = parse_exif_datetime(sub_ifd.get(tag))
        if captured_at is not None:
            return CaptureDate(captured_at=captured_at, source=source)
    return CaptureDate.unknown()


def _position(gps_ifd: Mapping[int, object]) -> PositionReading:
    """Transforma um IFD de GPS numa leitura, aplicando o RFC-032 §4.3 em ordem.

    Toda recusa abaixo é `unknown`, e cada uma é uma linha separada para que
    nenhuma possa ser "simplificada" dentro de outra:

    - sem IFD de GPS, ou sem latitude, ou sem longitude -- **nunca meia
      posição**;
    - uma referência ausente ou que não seja `N`/`S` (`E`/`W`): o hemisfério
      nunca é adivinhado;
    - uma tripla que não sejam três números, um racional com denominador zero,
      `NaN`, ou um componente negativo;
    - o marcador `0, 0`, **reconhecido pelo nome** em vez de deixado para uma
      checagem de faixa rejeitar por acaso -- `0, 0` está dentro de toda faixa;
    - qualquer coisa fora de `[-90, 90]` x `[-180, 180]`.
    """
    latitude = parse_gps_coordinate(
        gps_ifd.get(GPS_LATITUDE), gps_ifd.get(GPS_LATITUDE_REF), _HEMISPHERE_SIGNS[0]
    )
    longitude = parse_gps_coordinate(
        gps_ifd.get(GPS_LONGITUDE),
        gps_ifd.get(GPS_LONGITUDE_REF),
        _HEMISPHERE_SIGNS[1],
    )
    if latitude is None or longitude is None:
        return PositionReading.unknown()
    if is_null_island(latitude, longitude):
        return PositionReading.unknown()
    if abs(latitude) > MAX_LATITUDE or abs(longitude) > MAX_LONGITUDE:
        return PositionReading.unknown()
    return PositionReading(
        position=Position(latitude, longitude), source=PositionSource.EXIF_GPS
    )


def parse_gps_coordinate(
    raw: object, reference: object, signs: Mapping[str, int]
) -> float | None:
    """Graus decimais a partir de uma tripla DMS e da letra do hemisfério, ou `None`.

    `signs` mapeia as duas letras válidas deste eixo para o seu sinal. A letra
    é ASCII no arquivo, e o Pillow pode devolvê-la como `str` ou `bytes`,
    terminada em NUL de qualquer jeito.
    """
    sign = _hemisphere_sign(reference, signs)
    if sign is None:
        return None
    if not isinstance(raw, tuple | list) or len(raw) != 3:
        return None
    try:
        degrees, minutes, seconds = (float(part) for part in raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    parts = (degrees, minutes, seconds)
    if not all(math.isfinite(part) and part >= 0 for part in parts):
        return None
    return sign * (degrees + minutes / 60 + seconds / 3600)


def is_null_island(latitude: float, longitude: float) -> bool:
    """Se uma coordenada é o marcador de "sem fix" dos receptores, `0, 0`."""
    return (
        abs(latitude) < NULL_ISLAND_TOLERANCE_DEGREES
        and abs(longitude) < NULL_ISLAND_TOLERANCE_DEGREES
    )


def _hemisphere_sign(reference: object, signs: Mapping[str, int]) -> int | None:
    if isinstance(reference, bytes):
        try:
            reference = reference.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(reference, str):
        return None
    return signs.get(reference.strip("\x00 \t\r\n").upper())


def parse_exif_datetime(raw: object) -> datetime.datetime | None:
    """Interpreta uma string de data EXIF como um `datetime` ingênuo, ou `None`.

    `None` para tudo o que não for uma data utilizável, e quem chama
    simplesmente passa para o próximo elo da cadeia:

    - ausente, ou nem sequer texto;
    - o marcador `0000:00:00 00:00:00` que câmeras e exportadores escrevem
      quando o relógio nunca foi acertado. Filtrado pelo nome, e não deixado
      para o `strptime` rejeitar: engolir o erro de interpretação daria a
      resposta certa pelo motivo errado, e a próxima pessoa a afrouxar a
      interpretação o transformaria no ano 0 -- que o `datetime` nem consegue
      representar;
    - brancos e dois-pontos (`"    :  :     :  :  "`), o outro marcador que a
      especificação EXIF permite para uma data desconhecida;
    - qualquer outra coisa que o `strptime` rejeite: um mês impossível, um
      sufixo de fuso que algum software acrescenta, texto truncado.

    NUL e espaços em branco são removidos antes. Os valores ASCII do EXIF são
    terminados em NUL e muitas vezes preenchidos até uma largura fixa, e o
    Pillow devolve o terminador como parte da string.

    O resultado é ingênuo, sempre. O formato não tem fuso, e inventar um é o
    erro que o RFC-028 §5 existe para evitar.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None

    text = raw.strip("\x00 \t\r\n")
    if _is_placeholder(text):
        return None
    try:
        return datetime.datetime.strptime(text, EXIF_DATETIME_FORMAT)
    except ValueError:
        return None


def _is_placeholder(text: str) -> bool:
    """Se `text` é uma das grafias EXIF de "sem data"."""
    significant = text.replace(":", "").replace(" ", "")
    return not significant or set(significant) == {"0"}
