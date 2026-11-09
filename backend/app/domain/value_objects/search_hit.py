"""Portador que junta uma imagem ao quanto ela casou com uma busca."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.entities.image import Image


@dataclass(frozen=True)
class SearchHit:
    """Um resultado ranqueado: a imagem que casou, e com que força.

    A pontuação, deliberadamente, *não* é um campo de `Image`. Uma `Image` é
    a própria foto -- é congelada, compara por id e quer dizer a mesma coisa
    para todo chamador. Uma similaridade não quer dizer nada sem a consulta
    que a produziu: a mesma imagem pontua diferente em cada busca, e não
    pontuaria nada se fosse só listada. Guardar um número por consulta numa
    entidade por imagem faria dois valores `Image` do mesmo arquivo
    carregarem dados contraditórios e, ainda assim, serem iguais.

    Mora no Domain pelo mesmo motivo de `IndexingRecord`: faz parte do
    contrato da própria porta `ImageRepository`, então mantê-lo aqui deixa a
    porta autocontida na sua camada, em vez de fazer o Domain depender da
    Application para o próprio tipo de retorno.
    """

    image: Image
    similarity: float
    """Similaridade de cosseno em [-1, 1], em que 1 é a mesma direção.

    Não é reescalada para [0, 1] em nenhum ponto do caminho. Valores
    negativos são legítimos e querem dizer que os vetores apontam em
    direções opostas; dobrá-los para dentro destruiria a distinção entre
    "sem relação" (~0) e "oposto" (~-1). Se o número é comparável entre
    consultas é uma propriedade do espaço de embedding, não deste portador.
    """


# Toda implementação de `ImageRepository` define um método `list()`, que
# encobre o builtin `list` dentro do corpo da classe: uma anotação escrita
# como `-> list[SearchHit]` num método de uma dessas classes resolve para o
# método, não para o tipo. O alias é avaliado aqui, no escopo do módulo,
# onde `list` ainda quer dizer `list`, e é o mesmo objeto que
# `list[SearchHit]` para qualquer coisa que compare anotações.
SearchHits = list[SearchHit]
