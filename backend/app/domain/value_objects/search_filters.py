"""O recorte aplicado a uma busca semântica antes do ranqueamento (RFC-027 §9).

O RFC-025 deixou a busca global de propósito e nomeou a condição para
mudar isso: *"scoping search by collection is a change to what a
collection means, and needs the table, the foreign key and the ownership
rules first."* O RFC-027 entrega uma tabela e uma foreign key -- de
dispositivos, que são um fato físico, e não uma escolha de modelagem --,
então a condição está cumprida e o primeiro filtro pode existir.

A estrutura, e não só o filtro, é a entrega. O RFC-028 acrescentou um
intervalo de data de captura a este mesmo objeto; introduzir o mecanismo no
RFC-027 com um campo e estendê-lo com um segundo segue o que o RFC-024 fez
ao acrescentar `content_hash` ao `IndexMetadata` do RFC-020. O RFC-032 o
estendeu uma segunda vez, com um círculo -- e essa é a prova de que a
estrutura serve: cada eixo é um campo nomeado com um `WHERE` que dá para
ler, um plano que dá para medir e um teste de contrato que faz os três
repositórios concordarem, em vez de uma linguagem de consulta que o cliente
monta (RFC-032 §2.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.value_objects.date_range import DateRange
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.geo_circle import GeoCircle


@dataclass(frozen=True)
class SearchFilters:
    """Qual subconjunto do índice uma busca pode ranquear.

    Um `SearchFilters()` vazio quer dizer *nenhum recorte* e tem de produzir
    exatamente a consulta que o RFC-025 entregou -- não "uma lista IN
    vazia", que não casaria com nada e transformaria um argumento com
    default numa busca que devolve zero resultados em silêncio. Por isso o
    default é o conjunto vazio, e não `None`: há uma única representação de
    "sem filtro", e é a que todo chamador recebe por default.

    `frozenset`, e não uma lista ou uma tupla, porque o campo carrega um
    *conjunto* de dispositivos: a ordem não importa, duplicatas não
    importam, e o objeto tem de continuar hashable para manter o dataclass
    congelado como `Image` e `ImageId`.

    Os três campos recortam de forma independente e se combinam com AND:
    nomear um dispositivo, um intervalo e um círculo pede imagens naquele
    dispositivo *e* naquele intervalo *e* perto daquele ponto. Qualquer um
    pode faltar sem afetar os outros.
    """

    device_ids: frozenset[DeviceId] = field(default_factory=frozenset)
    """Restringe os resultados a imagens guardadas nestes dispositivos; vazio é todos.

    Um dispositivo desconectado continua sendo um membro legítimo. A busca
    ranqueia o que está *indexado*, e todo o argumento de produto dos
    dispositivos (RFC-027 §2.3) é que um acerto num disco guardado na gaveta
    -- informado como tal -- é uma resposta útil, não uma resposta quebrada.
    """

    captured_between: DateRange | None = None
    """Restringe os resultados a imagens cuja data de captura está neste intervalo.

    `None` quer dizer sem recorte por data, e é a *única* forma de dizer
    isso. Um intervalo ausente não pode ser representado como
    `[datetime.min, datetime.max)`: esse intervalo exclui toda imagem com
    data desconhecida, então um argumento com default esconderia em
    silêncio toda foto sem EXIF (RFC-028 §4.1) e acrescentaria uma cláusula
    à consulta que o RFC-025 entregou.

    Uma imagem cuja data de captura é desconhecida nunca casa com um
    intervalo, por mais largo que seja. Desconhecido nunca quer dizer "casa"
    (RFC-020).
    """

    taken_within: GeoCircle | None = None
    """Restringe os resultados a imagens cuja posição está neste círculo (RFC-032).

    `None` quer dizer sem recorte espacial, e é a única forma de dizer isso
    -- e não um círculo em volta do planeta, que acrescentaria uma cláusula
    à consulta que o RFC-025 entregou e descartaria em silêncio toda imagem
    sem coordenadas.

    Uma imagem cuja posição é desconhecida nunca casa com um círculo, por
    maior que seja (RFC-020). A posição comparada é a da aeronave, gravada
    pelo GPS, e não o ponto fotografado; é o raio que absorve a diferença
    (RFC-032 §2.2).
    """

    def is_empty(self) -> bool:
        """Diz se este filtro recorta alguma coisa.

        Tem nome, em vez de ficar a cargo do `if not filters.device_ids` de
        cada repositório, para que o segundo campo do RFC-028 não pudesse
        ser acrescentado enquanto uma implementação continuava verificando
        só o primeiro -- e foi atualizado na mesma mudança que o terceiro, do
        RFC-032, que é para isso que ele existe.
        """
        return (
            not self.device_ids
            and self.captured_between is None
            and self.taken_within is None
        )
