"""Onde na Terra uma foto foi tirada, e o que o arquivo dizia sobre isso (RFC-032).

Três valores, mantidos separados porque respondem a perguntas diferentes:

    Position         um ponto: latitude e longitude, com as faixas validadas
    PositionReading  o resultado de examinar um arquivo -- um ponto e sua
                     origem, ou `unknown`
    BoundingBox      uma área alinhada aos eixos: o viewport do mapa, e o
                     pré-filtro que um círculo de busca entrega ao banco
    PositionCell     um quadrado do mapa, e quantas fotos há nele

**O vocabulário é `position`, nunca `location`** (RFC-032 §4.5). Neste
código, *location* já quer dizer onde o *arquivo* está -- um ponto de
montagem e um caminho absoluto, resolvidos a cada requisição por
`ResolveImageLocationUseCase` -- e quem revisa nunca deve ter de perguntar
qual dos dois a palavra quer dizer.

**Sem unidade, de propósito.** Uma coordenada aqui é graus decimais como o
receptor GNSS os gravou, WGS 84, sem conversão de datum e sem unidade
própria além de "graus". Metros aparecem em um único lugar, o raio de um
círculo (`GeoCircle`), e em nenhum lugar um é convertido no outro, a não ser
pela distância de haversine que os compara.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain.exceptions import InvalidBoundingBoxError, InvalidPositionError
from app.domain.value_objects.position_source import PositionSource

MAX_LATITUDE = 90.0
MAX_LONGITUDE = 180.0


@dataclass(frozen=True)
class Position:
    """Um ponto na Terra, em graus decimais.

    As verificações de faixa são as mesmas que `ck_images_latitude_range` e
    `ck_images_longitude_range` impõem em SQL (RFC-032 §5). As duas existem
    de propósito: esta produz uma mensagem que uma pessoa consegue ler, e o
    CHECK é a parte que nenhum escritor futuro -- um `UPDATE` manual, um
    backfill descuidado -- consegue contornar.

    **`0, 0` é uma `Position` válida.** É um ponto real no Golfo da Guiné, e
    um clique no mapa ali é um centro de busca legítimo. Que o `0, 0` do
    *EXIF* seja um marcador de "sem sinal de satélite" é um fato sobre como
    os receptores gravam arquivos, e é reconhecido pelo nome onde os
    arquivos são lidos (`exif_capture_date.py`), não escondido aqui dentro
    de uma verificação de faixa.
    """

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        _require_coordinate("latitude", self.latitude, MAX_LATITUDE)
        _require_coordinate("longitude", self.longitude, MAX_LONGITUDE)


@dataclass(frozen=True)
class PositionReading:
    """O resultado de examinar um arquivo em busca de onde ele foi tirado.

    O `CaptureDate` da posição: sempre o resultado de um exame. "Nunca
    examinado" não é um `PositionReading` -- é a *ausência* de um, `None`
    onde se espera um `PositionReading | None` -- e essa assimetria é a
    distinção NULL / `'unknown'` do RFC-032 §4.4 expressa como tipo.

    Distinto de `Position` porque os dois são usados de formas diferentes:
    um `GeoCircle` é centrado num ponto, que não tem origem; um arquivo dá
    uma leitura, que pode não ter ponto.
    """

    position: Position | None
    source: PositionSource

    def __post_init__(self) -> None:
        if self.position is not None and not isinstance(self.position, Position):
            raise InvalidPositionError("A position reading needs a Position or None.")
        if not isinstance(self.source, PositionSource):
            raise InvalidPositionError("Position source must be a PositionSource.")
        if (self.position is None) != (self.source is PositionSource.UNKNOWN):
            raise InvalidPositionError(
                f"Position source {self.source.value!r} does not match "
                f"{'a missing' if self.position is None else 'a present'} "
                "position: 'unknown' is exactly the case with no position."
            )

    @classmethod
    def unknown(cls) -> PositionReading:
        """O arquivo foi examinado e não tem posição utilizável."""
        return cls(position=None, source=PositionSource.UNKNOWN)

    @property
    def latitude(self) -> float | None:
        return self.position.latitude if self.position is not None else None

    @property
    def longitude(self) -> float | None:
        return self.position.longitude if self.position is not None else None


@dataclass(frozen=True)
class BoundingBox:
    """Uma área alinhada aos eixos, cantos incluídos: `min <= value <= max` nos dois.

    Dois usuários, uma forma. `GET /images/map` recebe o viewport como uma,
    e `GeoCircle.bounding_box()` produz uma como o pré-filtro que deixa um
    índice ajudar uma busca por círculo.

    **`min > max` é recusado em qualquer eixo.** Na latitude, só pode
    significar cantos trocados. Na longitude, significa o mesmo -- ou um
    viewport que atravessa +/-180 graus, que o RFC-032 §7 declara sem
    suporte por enquanto. Os quatro números não distinguem os dois casos,
    então nenhum é adivinhado.

    Uma caixa de largura ou altura zero é válida e casa só com o que está
    exatamente sobre ela, como `DateRange(x, x)` é válido e não casa com
    nada.
    """

    min_latitude: float
    min_longitude: float
    max_latitude: float
    max_longitude: float

    def __post_init__(self) -> None:
        Position(self.min_latitude, self.min_longitude)
        Position(self.max_latitude, self.max_longitude)
        if self.min_latitude > self.max_latitude:
            raise InvalidBoundingBoxError(
                f"Map area min_lat {self.min_latitude} is north of max_lat "
                f"{self.max_latitude}."
            )
        if self.min_longitude > self.max_longitude:
            raise InvalidBoundingBoxError(
                f"Map area min_lon {self.min_longitude} is east of max_lon "
                f"{self.max_longitude}. Either the corners are swapped or the "
                "area crosses the 180th meridian, which is not supported."
            )

    def contains(self, position: Position) -> bool:
        """Se `position` está dentro da caixa, bordas incluídas."""
        return (
            self.min_latitude <= position.latitude <= self.max_latitude
            and self.min_longitude <= position.longitude <= self.max_longitude
        )


@dataclass(frozen=True)
class PositionCell:
    """Um quadrado do mapa: o centro, e quantas fotos caem nele (RFC-032 §7).

    O centro é o par de coordenadas arredondado ao número pedido de casas
    decimais, que é o meio do quadrado de tudo o que arredonda para ele --
    3 casas são cerca de 100 m. Uma célula só existe porque há pelo menos
    uma foto nela, então `count` nunca é zero.

    Mora no Domain ao lado de `Position` porque faz parte do contrato da
    porta `ImageRepository`, como `SearchHit`.
    """

    latitude: float
    longitude: float
    count: int


# Toda implementação de `ImageRepository` define um método `list()`, que
# encobre o builtin dentro do corpo da classe; `-> list[PositionCell]` escrito
# num dos métodos dela resolveria para o método. Avaliado aqui, no escopo do
# módulo, pelo mesmo motivo de `SearchHits`.
PositionCells = list[PositionCell]


def validate_position_fields(
    latitude: float | None,
    longitude: float | None,
    source: PositionSource | None,
) -> None:
    """Impõe as regras de pareamento a uma posição guardada em três campos soltos.

    Usado por `Image`, que guarda as metades separadas porque são três
    colunas. A regra que pesa é a primeira -- **nunca meia posição**
    (RFC-032 §4.3): uma latitude sem longitude passaria em toda verificação
    de faixa e não localizaria nada. `ck_images_position_pairing` diz o
    mesmo em SQL.

    `source is None` é válido aqui e em nenhum lugar de `PositionReading`:
    em `Image`, quer dizer que a linha nunca foi examinada, e então também
    não pode haver coordenadas.
    """
    if (latitude is None) != (longitude is None):
        raise InvalidPositionError(
            "A position needs both a latitude and a longitude; got only the "
            f"{'longitude' if latitude is None else 'latitude'}."
        )
    position = (
        Position(latitude, longitude)
        if latitude is not None and longitude is not None
        else None
    )
    if source is None:
        if position is not None:
            raise InvalidPositionError("A position needs the source it was read from.")
        return
    PositionReading(position=position, source=source)


def _require_coordinate(name: str, value: object, limit: float) -> None:
    """Um número finito dentro de `[-limit, limit]`, ou `InvalidPositionError`.

    `bool` é recusado, embora seja subclasse de `int`: `True` como latitude
    é um bug de quem chama, não o equador mais um grau.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidPositionError(f"Position {name} must be a number.")
    if not math.isfinite(value):
        raise InvalidPositionError(f"Position {name} must be finite, got {value}.")
    if not -limit <= value <= limit:
        raise InvalidPositionError(
            f"Position {name} {value} is outside [-{limit:g}, {limit:g}]."
        )
