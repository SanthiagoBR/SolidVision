"""Formatos das respostas das buscas (RFC-026 §5.2, RFC-030, RFC-038).

O único lugar do código autorizado a decidir como um resultado de busca
aparece na resposta, e as decisões que ele toma são, na maioria, sobre o que
deixar de fora.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict
from uuid import UUID

from pydantic import BaseModel, Field

from app.application.use_cases.resolve_image_location import LocatedImage
from app.domain.value_objects.search_hit import SearchHit
from app.presentation.schemas.image_schema import ImageSchema, image_fields


class SearchResultSchema(ImageSchema):
    """Uma imagem ranqueada, como o cliente a vê: a imagem, mais o quanto casou.

    **O caminho agora é publicado, e o RFC-030 §2 diz por quê.** O RFC-026 o
    deixou de fora com dois argumentos, registrados aqui em vez de apagados:

    > *"Publicá-lo exporia o layout do sistema de arquivos do servidor a todo
    > chamador, e entregaria aos clientes um identificador que muda sempre
    > que um arquivo se move."*

    O primeiro se desfez. Numa implantação local-first, o sistema de
    arquivos do servidor *é* o do usuário, e uma busca que termina num nome
    de arquivo e num UUID achou uma foto sem dizer a ninguém onde ela está
    -- faltava o último passo do produto. O segundo continua valendo, e
    molda o que é publicado: `id` continua sendo o único identificador, e os
    caminhos são exibição. Nenhuma rota aceita um caminho de volta.

    O formato é `ImageSchema` -- o corpo de `GET /images/{id}`, que esta
    docstring um dia prometeu que o "RFC-027" acrescentaria -- mais
    `similarity`, que só existe dentro de uma consulta (RFC-025 §4.1).

    **Um acerto num disco desconectado é uma resposta completa.**
    `device.connected` é falso, `absolute_path` é nulo, e `device.label` e
    `relative_path` dizem "HD3, fotos/2018/junho" -- o que, para quem tem
    vinte discos, é a parte cara da pergunta (RFC-030 §4.1).

    `captured_at` e `capture_source` são atributos da fotografia, idênticos
    para todo chamador e toda consulta, e as duas coisas que tornam legível
    um resultado filtrado por data (RFC-028 §12). `latitude`, `longitude` e
    `position_source` fazem o mesmo para um círculo (RFC-032).
    """

    similarity: float = Field(
        description="Cosine similarity in [-1, 1]; 1 is identical direction"
    )

    @classmethod
    def from_hit(cls, hit: SearchHit, located: LocatedImage) -> SearchResultSchema:
        """Leva um acerto do domínio e a sua localização à resposta, sem acrescentar.

        `located` precisa descrever `hit.image`; o pareamento é conferido,
        porque um descompasso publicaria a pontuação de uma foto sob o caminho
        de outra, e nada mais adiante conseguiria notar.

        `similarity` passa adiante sem arredondar e sem limitar. Reescalá-la
        para [0, 1] para uma barra de progresso destruiria a distinção entre
        "sem relação" (~0) e "oposto" (~-1) que o RFC-025 §14 manteve de
        propósito, e esta camada é a que tem menos informação sobre o que o
        número quer dizer: ela não sabe que a pontuação é um cosseno, nem que
        houve um vetor no meio. Um cliente que queira uma porcentagem pode
        calcular uma a partir da distribuição medida.

        `captured_at` passa adiante ingênuo, e o Pydantic serializa um
        `datetime` ingênuo como ISO 8601 sem `Z` e sem offset. Essa é a saída
        de que o RFC-028 §5 precisa, e ela é fixada por um teste, e não
        presumida: um `Z` aqui diria a todo cliente que o relógio da câmera
        estava em UTC, desfazendo o motivo de a coluna não ter fuso.
        """
        if located.image.id != hit.image.id:
            raise ValueError(
                f"Location for {located.image.id} paired with hit {hit.image.id}"
            )
        return cls(similarity=hit.similarity, **image_fields(located))


class RankedFields(TypedDict):
    """Os argumentos nomeados comuns a toda resposta ranqueada, tipados para `**`."""

    limit: int
    results: list[SearchResultSchema]
    excluded_unknown_date: int | None
    excluded_unknown_position: int | None


def ranked_fields(
    limit: int,
    hits: Sequence[SearchHit],
    locations: Sequence[LocatedImage],
    excluded_unknown_date: int | None = None,
    excluded_unknown_position: int | None = None,
) -> RankedFields:
    """Embrulha um ranking sem reordená-lo, filtrá-lo nem truncá-lo.

    A ordem é a do repositório, que é a do PostgreSQL, que é a que o índice
    vetorial produziu. Reordenar aqui por `similarity` na melhor das
    hipóteses a reproduziria e, na pior, discordaria dela nos empates, que o
    repositório desempata por id de propósito.

    `locations` é a resposta de `ResolveImageLocationUseCase` para os mesmos
    acertos, na mesma ordem -- ele mantém a ordem por contrato, então os
    dois são pareados por posição, e `strict=True` recusa uma diferença de
    tamanho em vez de descartar a cauda do ranking.
    """
    return {
        "limit": limit,
        "results": [
            SearchResultSchema.from_hit(hit, located)
            for hit, located in zip(hits, locations, strict=True)
        ],
        "excluded_unknown_date": excluded_unknown_date,
        "excluded_unknown_position": excluded_unknown_position,
    }


class RankedImagesSchema(BaseModel):
    """Um ranking do índice, qualquer que tenha sido a consulta (RFC-038 §4.1).

    O corpo de `POST /images/search/by-image` tal como é, e a base dos outros
    dois: a busca por texto acrescenta a `query` que devolve, e *"Parecidas
    com esta"*, o id da foto de onde partiu. Um formato e um conjunto de
    regras para os três, para que um cliente os leia do mesmo jeito.

    `limit` devolve o valor de fato usado, não o pedido, que em geral nem é
    enviado: sem ele, quem recebe três resultados não consegue distinguir
    "só havia três imagens" de "o default é menor do que eu supunha".
    """

    limit: int = Field(description="The page size actually applied")
    results: list[SearchResultSchema] = Field(
        description="Matches, most similar first, in the repository's order"
    )
    excluded_unknown_date: int | None = Field(
        description=(
            "How many indexed images the capture-date filter left out because "
            "their date is unknown, counted over the whole index under the "
            "other filters rather than over this page. Null when no date "
            "filter was sent."
        )
    )
    excluded_unknown_position: int | None = Field(
        description=(
            "How many indexed images the near_lat/near_lon/radius_m circle left "
            "out because they have no position, counted over the whole index "
            "under the other filters rather than over this page. Null -- not "
            "0 -- when no circle was sent: 0 would mean the circle hid nothing."
        )
    )

    @classmethod
    def from_ranking(
        cls,
        limit: int,
        hits: Sequence[SearchHit],
        locations: Sequence[LocatedImage],
        excluded_unknown_date: int | None = None,
        excluded_unknown_position: int | None = None,
    ) -> RankedImagesSchema:
        """A resposta de uma busca sem texto a devolver: a de uma imagem."""
        return RankedImagesSchema(
            **ranked_fields(
                limit, hits, locations, excluded_unknown_date, excluded_unknown_position
            )
        )


class SearchResponseSchema(RankedImagesSchema):
    """O corpo de uma busca por texto bem-sucedida.

    `query` devolve o que o cliente enviou, nunca o que foi pedido ao
    modelo. A string que o CLIP de fato codifica é o template do RFC-023 em
    volta de uma consulta possivelmente traduzida -- para uma entrada em
    português ela volta como `a photo of . a rural property with a lake`,
    com o `". "` na frente e tudo (RFC-025 §11.5). Publicar isso tornaria um
    artefato interno do caminho de tradução parte do contrato, e mudaria a
    resposta no dia em que o template mudasse.

    Desde o RFC-038, o resto é de `RankedImagesSchema`, campo a campo: a
    resposta não mudou, só o lugar onde seus campos são declarados.
    """

    query: str = Field(description="The query exactly as the client sent it")

    @classmethod
    def from_hits(
        cls,
        query: str,
        limit: int,
        hits: Sequence[SearchHit],
        locations: Sequence[LocatedImage],
        excluded_unknown_date: int | None = None,
        excluded_unknown_position: int | None = None,
    ) -> SearchResponseSchema:
        """Embrulha um ranking sem reordená-lo; veja `ranked_fields()`."""
        return cls(
            query=query,
            **ranked_fields(
                limit, hits, locations, excluded_unknown_date, excluded_unknown_position
            ),
        )


class SimilarImagesResponseSchema(RankedImagesSchema):
    """O corpo de *"Parecidas com esta"*: `GET /images/{id}/similar` (RFC-038).

    `image_id` é a foto de onde o ranking partiu, que nunca está entre os
    `results`: viria em primeiro, com similaridade 1. Uma cópia dela -- os
    mesmos bytes sob outro id -- está, e vem perto do topo.
    """

    image_id: UUID = Field(
        description="The photo whose stored embedding was the query; not in results"
    )

    @classmethod
    def from_source(
        cls,
        image_id: UUID,
        limit: int,
        hits: Sequence[SearchHit],
        locations: Sequence[LocatedImage],
        excluded_unknown_date: int | None = None,
        excluded_unknown_position: int | None = None,
    ) -> SimilarImagesResponseSchema:
        return cls(
            image_id=image_id,
            **ranked_fields(
                limit, hits, locations, excluded_unknown_date, excluded_unknown_position
            ),
        )
