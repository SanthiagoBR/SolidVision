"""Onde estão as fotos, em células que um mapa consegue desenhar (RFC-032 §7).

Um mapa não se desenha com o que a busca devolve. A busca entrega no máximo
`MAX_SEARCH_LIMIT` resultados ranqueados, e um mapa feito com eles mostraria onde
estão as cem fotos *mais parecidas com a consulta* -- o que não responde "onde
estão as minhas fotos". E buscar 100 000 pontos para o cliente agrupar faz a
coleção atravessar a rede para responder a uma pergunta que um `GROUP BY` responde
no banco.

**Este caso de uso ignora o texto da consulta, e essa é a decisão que precisa
ficar escrita**, porque a alternativa parece melhor e não é: um mapa que
refletisse a consulta só poderia agregar o top K, e o top 100 de um índice de
100 000 imagens não descreve nada da geografia. O mapa mostra onde estão as fotos
*que os filtros permitem*; ranquear continua sendo trabalho da busca. Duas
perguntas, dois endpoints.

Desde o RFC-040 (§4.1), a resposta diz também quantas fotos um intervalo de datas
deixou fora do mapa por não terem data: a contagem que a busca já faz, pela mesma
função, sobre o universo do mapa.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from app.application.use_cases.search_images import count_hidden_by_unknown_date
from app.domain.repositories.image_repository import ImageRepository
from app.domain.value_objects.position import BoundingBox, PositionCell
from app.domain.value_objects.search_filters import SearchFilters

DEFAULT_MAP_PRECISION = 3
"""Casas decimais de um grau quando o cliente não pede: células de ~100 m.

O tamanho de célula com que o RFC-032 §2.3 mediu o piloto -- 40 fotos em 24
células -- e a ordem de grandeza do deslocamento da aeronave que o raio da busca
precisa absorver; assim, uma célula é mais ou menos tão precisa quanto uma
posição merece.
"""

MAX_MAP_PRECISION = 6
"""A célula mais fina oferecida: ~0,1 m, já abaixo do que o GNSS de um drone sabe.

Um pedido acima disso é atendido nesta precisão e o diz por `precision_applied`,
como um pedido fino demais é respondido em todo o resto deste caso de uso -- a
resposta ecoa o que foi aplicado, em vez de recusar um número que só poderia
tornar as células sem sentido.
"""


@dataclass(frozen=True)
class PositionMap:
    """Uma resposta do mapa: as células, a precisão do corte e o que falta.

    `precision_applied` ecoa a precisão de fato usada, como `limit` faz na
    busca (RFC-026): sem ele, um cliente que pediu células de 100 m e recebeu
    células de 10 km não teria como distinguir uma coleção esparsa de uma
    resposta reduzida.

    `excluded_unknown_position` é o número de fotos sob os mesmos filtros que
    não têm coordenadas e, por isso, não podem estar em mapa nenhum -- o número
    do RFC-032 §6.2, no lugar em que o mapa precisa dele.

    `excluded_unknown_date` é o seu gêmeo para um intervalo de datas (RFC-040
    §4.1): quantas fotos sob o filtro de dispositivo não têm data de captura e,
    por isso, foram escondidas pelo intervalo -- `None` sem intervalo, como na
    busca (RFC-028 §8.2). Nenhuma das contagens se restringe à área: as duas são
    da coleção sob os filtros e não mudam a cada arrasto do mapa. Uma foto sem
    data e sem posição entra só nesta: o universo da contagem de posição tem a
    cláusula de data, na qual ela não passa.
    """

    precision_requested: int
    precision_applied: int
    cells: list[PositionCell]
    excluded_unknown_position: int
    excluded_unknown_date: int | None


class AggregatePositionsUseCase:
    """Agrupa as fotos de uma área visível em no máximo `max_cells` células.

    `max_cells` é injetado de `settings.max_map_cells` pela raiz de composição,
    nunca lido das configurações aqui, pelo mesmo motivo do `default_limit`,
    vizinho de `MAX_SEARCH_LIMIT`: a camada de aplicação não importa
    configuração, e `test_application_architecture.py` garante isso.
    """

    def __init__(self, repository: ImageRepository, max_cells: int) -> None:
        if max_cells < 1:
            raise ValueError(f"max_cells must be at least 1, got {max_cells}")
        self._repository = repository
        self._max_cells = max_cells

    def execute(
        self,
        area: BoundingBox,
        filters: SearchFilters | None = None,
        precision: int | None = None,
    ) -> PositionMap:
        """Devolve as células de `area` sob `filters`, engrossando até caberem.

        **O universo é o dos filtros.** Imagens com embedding -- as que uma
        busca poderia devolver -- nos dispositivos pedidos, no intervalo de
        datas pedido, com posição dentro de `area`. Um círculo em `filters` é
        descartado em vez de aplicado: a área visível é a área do mapa, e
        "perto daqui" é a pergunta da busca (RFC-032 §9).

        **Acima de `max_cells`, a precisão cai uma casa decimal por vez** até as
        células caberem, e a resposta diz em que precisão parou. Células mais
        grossas não escondem nenhuma foto -- todas continuam contadas, num
        quadrado maior --, enquanto truncar a lista esconderia lugares
        inteiros. Cada passo é uma consulta agrupada; a maioria das áreas
        visíveis cabe na primeira.

        Na precisão 0 (células de 1 grau) não há nada mais grosso para onde
        recuar, e todas as células são devolvidas, mesmo que passem de
        `max_cells`: no máximo 181 x 361 pela geometria e, para uma coleção que
        cabe num país, um punhado. Passar do teto ali é a falha honesta;
        descartar fotos para respeitá-lo não seria.

        **As fotos que um intervalo de datas escondeu são contadas como a busca
        as conta** (RFC-040 §4.1): `count_hidden_by_unknown_date()`, a função
        que os dois casos de uso de busca chamam, sobre este universo, que não
        tem círculo. Sem intervalo, não há consulta, e o valor é `None`.
        """
        requested = DEFAULT_MAP_PRECISION if precision is None else precision
        if requested < 0:
            raise ValueError(f"precision must not be negative, got {requested}")
        universe = dataclasses.replace(filters or SearchFilters(), taken_within=None)

        applied = min(requested, MAX_MAP_PRECISION)
        while True:
            limit = self._max_cells + 1 if applied > 0 else None
            cells = self._repository.aggregate_positions(universe, area, applied, limit)
            if len(cells) <= self._max_cells or applied == 0:
                break
            applied -= 1

        return PositionMap(
            precision_requested=requested,
            precision_applied=applied,
            cells=cells,
            excluded_unknown_position=self._repository.count_unknown_position(universe),
            excluded_unknown_date=count_hidden_by_unknown_date(
                self._repository, universe
            ),
        )
