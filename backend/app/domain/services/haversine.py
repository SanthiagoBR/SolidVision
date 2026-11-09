"""Distância de grande círculo entre duas posições, em metros (RFC-032 §6).

**Uma implementação**, compartilhada pelos dois dublês de `ImageRepository`
em processo, e uma constante compartilhada com a cláusula do PostgreSQL que
calcula a mesma fórmula em SQL. Duas haversines escritas à mão seriam duas
chances de se afastar do banco, e o teste de contrato prende as três a uma
resposta só -- o que só funciona se o raio da Terra for o mesmo número em
todo lugar.

Haversine, e não a lei esférica dos cossenos, porque ela continua exata em
distâncias curtas: a lei dos cossenos tira o `acos` de um valor a um erro de
arredondamento de 1 para dois pontos a metros um do outro, e as fotos para as
quais este filtro existe estão a dezenas de metros umas das outras.

Uma esfera, e não o elipsoide WGS 84. O erro fica abaixo de 0,5% da
distância, e a incerteza que este filtro precisa absorver é o deslocamento
entre a aeronave e o chão fotografado -- dezenas a centenas de metros
(RFC-032 §2.2) -- e não o achatamento do planeta.
"""

from __future__ import annotations

import math

from app.domain.value_objects.position import Position

EARTH_RADIUS_M = 6_371_008.8
"""O raio médio da Terra segundo a IUGG, em metros.

Importado por `PostgresImageRepository` para a expressão SQL dele, nunca
redigitado lá: um teste de contrato cujas três implementações discordassem
pela diferença entre 6371 km e 6378 km falharia só para círculos desenhados
perto da borda, que é a falha mais difícil de ler.
"""


def haversine_m(first: Position, second: Position) -> float:
    """Devolve a distância de grande círculo entre duas posições, em metros.

    A diferença de longitude é tirada *antes* da conversão para radianos, e a
    expressão do PostgreSQL faz o mesmo. As duas ordens são iguais em
    aritmética exata e diferem nos últimos bits de um float; fazê-las
    coincidir mantém os dublês em memória e o banco de acordo para um ponto
    quase exatamente na borda de um círculo.

    `min(1.0, ...)` protege o `asin` de um erro de arredondamento que empurre
    o argumento dele um fio acima de 1 para pontos antípodas.
    """
    first_latitude = math.radians(first.latitude)
    second_latitude = math.radians(second.latitude)
    latitude_delta = math.radians(second.latitude - first.latitude)
    longitude_delta = math.radians(second.longitude - first.longitude)

    h = _squared_sine_of_half(latitude_delta) + math.cos(first_latitude) * math.cos(
        second_latitude
    ) * _squared_sine_of_half(longitude_delta)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _squared_sine_of_half(angle: float) -> float:
    """`sin(angle / 2)` ao quadrado, por multiplicação -- como o SQL o calcula."""
    sine = math.sin(angle / 2.0)
    return sine * sine
