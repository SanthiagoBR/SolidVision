"""Derivação determinística da identidade de imagens descobertas no sistema de arquivos.

O UUID de namespace abaixo foi gerado uma vez via `uuid.uuid4()` e agora está
congelado para sempre. Regerá-lo mudaria em silêncio o `ImageId` produzido para
todo arquivo já indexado, fazendo todos parecerem "novos". Nunca o regere, aqui
ou num RFC posterior, sem uma estratégia de migração explícita e documentada.

O RFC-027 mudou a *string* que alimenta esse namespace e, deliberadamente, não
mudou o próprio namespace. O que se fazia hash antes era o caminho absoluto, que
no Windows contém uma letra de unidade -- e a letra de unidade é função da ordem
de montagem, não do disco, então o mesmo arquivo intocado produzia um id
diferente sempre que por acaso montava como `F:` em vez de `D:`, duplicando
todas as linhas e repetindo horas de inferência (RFC-027 §2.1). O que se faz
hash agora é o dispositivo mais o caminho relativo ao ponto de montagem desse
dispositivo, e nenhum dos dois se move sozinho.

Preservar o namespace nessa mudança é intencional. Se o namespace e a string de
entrada mudassem ambos, um id antigo não poderia mais ser reconhecido como
vindo do esquema antigo.
"""

from __future__ import annotations

import uuid

from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath

SOLIDVISION_PATH_NAMESPACE = uuid.UUID("5dc64f53-522e-4303-951e-ee6123b10dd8")


def compute_image_id(device_id: DeviceId, relative_path: ImagePath) -> ImageId:
    """Deriva um `ImageId` determinístico de um dispositivo e de um caminho nele.

    O mesmo arquivo no mesmo disco sempre dá o mesmo id, entre execuções, entre
    máquinas e entre letras de unidade, porque nenhuma das entradas contém um
    ponto de montagem. `ImagePath.__str__()` normaliza os separadores numa forma
    posix única antes do hash, então um caminho construído a partir de uma
    string do Windows e um construído a partir de uma string posix concordam.

    Renomear ou mover um arquivo *dentro* de um dispositivo muda o seu caminho
    relativo e, portanto, o seu id -- isso produz um novo registro de imagem em
    vez de acompanhar o histórico através de renomeações, o que é uma limitação
    aceita para o escopo atual. Movê-lo para outro disco muda o id pela mesma
    razão.
    """
    return ImageId(
        uuid.uuid5(SOLIDVISION_PATH_NAMESPACE, f"{device_id}/{relative_path}")
    )
