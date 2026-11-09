"""Um ponto e um raio: "fotos tiradas perto daqui" (RFC-032 §6).

**A consulta é um círculo, nunca um ponto.** O usuário não sabe coordenadas;
ele reconhece um lugar quando o vê num mapa, então a busca é um clique e uma
distância -- e a distância faz parte da pergunta, e não é um detalhe de
implementação (RFC-032 §2.2). A coordenada no arquivo é onde a *aeronave*
estava, a dezenas ou centenas de metros do que ela fotografou, e o raio é o
que absorve isso.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain.exceptions import InvalidGeoCircleError
from app.domain.services.haversine import EARTH_RADIUS_M, haversine_m
from app.domain.value_objects.position import (
    MAX_LATITUDE,
    MAX_LONGITUDE,
    BoundingBox,
    Position,
)

_BOX_MARGIN = 1e-9
"""Folga relativa no raio da caixa do pré-filtro, para que ela só possa sobrar.

A caixa precisa conter o círculo inteiro, ou esconde fotos que a distância
exata teria mantido. Em aritmética exata ela contém; em ponto flutuante, um
ponto bem na borda do círculo poderia cair um erro de arredondamento fora
dela. Um bilionésimo do raio -- um micrômetro num quilômetro -- elimina isso
sem deixar entrar nada que a cláusula exata depois precise descartar em
números perceptíveis.
"""

_BOX_PAD_DEGREES = 1e-9
"""Folga absoluta em cada lado da caixa: cerca de 0,1 mm.

A margem relativa some para um raio pequeno, e o arredondamento contra o
qual ela protege está nos *graus*, cujo último bit perto de 90 é ~1e-14.
Cinto e suspensório, e de graça: um décimo de milímetro não deixa entrar
nada.
"""

_MAX_HALF_WIDTH_SINE = 1 - 1e-6
"""Acima disto, o `asin` é íngreme demais para confiar, e a caixa é descartada.

A meia-largura em longitude é `asin(sin(d) / cos(latitude))`, e o `asin` tem
inclinação infinita em 1: para um círculo que quase alcança um polo, um erro
de arredondamento no argumento vira um erro grande na largura. Esses
círculos estão a um fio do caso do polo, que de todo jeito não tem caixa.
"""


@dataclass(frozen=True)
class GeoCircle:
    """Toda posição a até `radius_m` metros de `center`, borda incluída.

    **`radius_m` precisa ser positivo e finito, e um raio zero é recusado**
    -- enquanto `DateRange(x, x)` é aceito como intervalo vazio. A diferença
    é deliberada (RFC-032 §6): "de X até X" é o que um cliente que calcula
    intervalos aritmeticamente produz de forma legítima, enquanto nenhum
    clique num mapa produz um círculo de raio zero. Recusar diz isso;
    responder com nada pareceria exatamente "não há foto aqui".

    **Sem limite superior.** "Em qualquer lugar do Brasil" é um pedido
    legítimo; o que um raio grande muda é a seletividade, e não a correção.
    Um raio maior que meio planeta simplesmente contém todas as posições.

    **Metros, em todo lugar.** Neste value object, no SQL e no parâmetro
    HTTP: duas unidades e uma conversão entre elas é um bug que nenhum teste
    percebe até ela se inverter (RFC-032 §9).

    O mínimo da aplicação (`MIN_RADIUS_M`) não é verificado aqui. Ele é
    política do produto sobre os deslocamentos da aeronave, e não uma
    propriedade dos círculos, e mora na camada Application, onde é injetado
    a partir das configurações.
    """

    center: Position
    radius_m: float

    def __post_init__(self) -> None:
        if not isinstance(self.center, Position):
            raise InvalidGeoCircleError("A search circle needs a Position centre.")
        radius = self.radius_m
        if isinstance(radius, bool) or not isinstance(radius, int | float):
            raise InvalidGeoCircleError("A search radius must be a number of metres.")
        if not math.isfinite(radius) or radius <= 0:
            raise InvalidGeoCircleError(
                f"A search radius must be a positive number of metres, got {radius}."
            )

    def contains(self, position: Position) -> bool:
        """Se `position` está dentro do raio, pela distância de haversine exata.

        Recebe uma `Position`, nunca `None`. Se uma posição desconhecida "está
        perto" de alguma coisa não é uma pergunta que este objeto responde: a
        regra do RFC-020 é que desconhecido nunca casa, e todo chamador
        precisa dizer isso explicitamente, em vez de herdá-lo de um padrão
        daqui.
        """
        return haversine_m(self.center, position) <= self.radius_m

    def bounding_box(self) -> BoundingBox | None:
        """A caixa do pré-filtro que contém o círculo, ou `None` quando não há.

        **Descartável pela correção.** A caixa existe só para que um índice
        possa restringir as linhas antes de a distância exata rodar, e é a
        distância que responde. Dois círculos não têm caixa alinhada aos
        eixos correta na forma `min <= x <= max`, e para eles isto devolve
        `None`, para que só a cláusula exata seja emitida -- mais lento, e
        certo:

        - **um círculo que contém um polo**: toda longitude está dentro dele,
          e a meia-largura em longitude, `asin(sin(d) / cos(latitude))`, não
          tem valor quando `d` alcança o polo;
        - **um círculo que cruza o meridiano 180**: a caixa dele daria a
          volta, o que `min <= longitude <= max` não consegue expressar --
          uma caixa invertida não casaria com nada.

        Para uma coleção no Brasil nenhum dos dois jamais acontece, e é
        exatamente por isso que precisa ficar escrito: o caso que nunca
        dispara é o que ninguém testa.

        A meia-largura em longitude usa a fórmula exata da calota esférica, e
        não `d / cos(latitude)`, que é uma aproximação que fica aquém -- e
        uma caixa que fica aquém esconde fotos.
        """
        angular = self.radius_m / EARTH_RADIUS_M * (1 + _BOX_MARGIN)
        latitude = math.radians(self.center.latitude)
        south = latitude - angular
        north = latitude + angular
        if north >= math.pi / 2 or south <= -math.pi / 2:
            return None

        half_width_sine = math.sin(angular) / math.cos(latitude)
        if half_width_sine > _MAX_HALF_WIDTH_SINE:
            return None
        half_width = math.degrees(math.asin(half_width_sine)) + _BOX_PAD_DEGREES
        west = self.center.longitude - half_width
        east = self.center.longitude + half_width
        if west < -MAX_LONGITUDE or east > MAX_LONGITUDE:
            return None

        return BoundingBox(
            min_latitude=max(-MAX_LATITUDE, math.degrees(south) - _BOX_PAD_DEGREES),
            min_longitude=west,
            max_latitude=min(MAX_LATITUDE, math.degrees(north) + _BOX_PAD_DEGREES),
            max_longitude=east,
        )
