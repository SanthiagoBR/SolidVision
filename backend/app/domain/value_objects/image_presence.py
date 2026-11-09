"""Se as linhas de um dispositivo ainda estão no disco dele (RFC-046).

Uma linha é **ausente** quando o último scan do disco inteiro não listou o
arquivo dela: `images.missing_since` guarda a data desse scan, e toda leitura
que serve a interface esconde a linha. A linha, o vetor e a thumbnail ficam,
para que o próximo scan que liste o arquivo de novo a desmarque sem
inferência (RFC-046 §3). Só a purga apaga, por um botão.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass

from app.domain.value_objects.image_id import ImageId


@dataclass(frozen=True, slots=True)
class ImagePresence:
    """Uma linha de um dispositivo, como a varredura precisa dela (RFC-046 §5.5).

    O id, que a varredura compara com os ids da listagem; o caminho relativo
    ao dispositivo, em posix como gravado, que ela compara com as pastas que
    não puderam ser lidas; e se a linha já está marcada.

    `slots`, pela razão de `ChangeSignals` tê-los: um objeto por linha, e o
    maior disco do usuário tem 116.396 linhas.
    """

    image_id: ImageId
    relative_path: str
    missing: bool


Presences = Iterator[ImagePresence]
"""O que `list_presence()` devolve: uma por linha do dispositivo, sem ordem.

**Um fluxo, e não uma lista.** A varredura olha cada linha uma vez, e uma
lista das 116.396 linhas do maior disco do usuário -- cada uma com o id e o
caminho -- era a maior parte dos 78,7 MB que a primeira medição achou, contra
um critério de 50 (RFC-046 §10). Um alias, como `ListedImages` é.
"""


@dataclass(frozen=True)
class MissingImages:
    """Quantas linhas de um dispositivo estão marcadas, e desde quando (RFC-046 §5.8).

    `since` é a marca mais antiga: o cartão diz *"312 fotos não encontradas no
    scan de 12/03"*, e o scan que primeiro deixou de achar qualquer uma delas
    é a data a partir da qual essa frase é verdadeira.
    """

    count: int
    since: datetime.datetime


@dataclass(frozen=True)
class PurgedImages:
    """O que uma purga apagou: quantas linhas, e as thumbnails para as quais apontavam.

    `count` inclui as linhas que não tinham thumbnail; `thumbnail_paths` são os
    locais no store das que tinham, para o store apagar depois que as linhas
    se forem (RFC-046 §5.7).
    """

    count: int
    thumbnail_paths: tuple[str, ...]
