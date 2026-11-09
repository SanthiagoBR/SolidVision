"""Onde as imagens indexadas estão agora, perguntado uma vez por disco (RFC-030 §4).

Duas rotas precisam da mesma resposta -- `GET /images/search` para cada
resultado de uma página, `GET /images/{id}` para uma imagem -- e
`POST /images/{id}/reveal` precisa dela antes de poder abrir qualquer coisa.
Nenhuma delas pode calculá-la sozinha:

* não a rota, porque "este disco está plugado, e onde" é exatamente o tipo de
  regra que o `AI_Context.md` mantém fora da Presentation;
* não o `SearchImagesUseCase`, porque resolver um local não é buscar. Isso
  decora um resultado depois do fato, e pô-lo dentro da busca deixaria os
  outros dois casos de uso ou duplicando-o ou importando um caso de uso para
  pegar emprestado um método privado.

Então é um caso de uso próprio, pequeno, composto pelas mesmas duas portas
que o `CreateIndexingJobUseCase` já combina: o `DeviceRepository`, que sabe o
nome de cada disco, e o `DeviceLocator`, que sabe onde ele está montado neste
instante.

**Nada aqui é persistido e nada aqui vai para cache.** O `absolute_path` vive
no `Image` devolvido pela duração de um pedido e nunca é escrito (RFC-027 §7)
-- a resposta está errada a partir do momento em que o usuário puxa o cabo, e
nada avisa este processo quando isso acontece.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.entities.image import Image
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.services.device_locator import DeviceLocator
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_path import ImagePath


@dataclass(frozen=True)
class LocatedImage:
    """Uma imagem, o disco em que ela está, e se esse disco está aqui agora.

    `connected` é derivado de `image.absolute_path`, em vez de guardado ao
    lado dele, para que os dois não possam discordar: uma imagem com caminho
    num disco desplugado, ou um disco plugado cuja imagem não tem caminho,
    não são estados que este tipo consegue representar.
    """

    image: Image
    """A imagem, com `absolute_path` preenchido quando o dispositivo está montado."""

    device: Device

    @property
    def connected(self) -> bool:
        """Se o disco da imagem estava montado quando o local foi resolvido.

        `False` é uma resposta, não uma falha: "está no HD3, em 2018/junho" é
        o resultado que o produto existe para dar sobre um disco na gaveta
        (RFC-030 §4.1).
        """
        return self.image.absolute_path is not None


class ResolveImageLocationUseCase:
    """Junta a cada imagem o seu dispositivo, e o caminho absoluto se montado."""

    def __init__(
        self, device_repository: DeviceRepository, device_locator: DeviceLocator
    ) -> None:
        self._devices = device_repository
        self._locator = device_locator

    def execute(self, images: Sequence[Image]) -> list[LocatedImage]:
        """Devolve um `LocatedImage` por imagem, na ordem dada.

        **O sistema operacional é consultado uma vez por dispositivo
        distinto, não uma vez por imagem.** `MountedDeviceLocator.mount_point()`
        enumera todo volume montado a cada chamada e se recusa, de propósito, a
        guardar cache (RFC-027 §7), então uma página de dez resultados
        espalhados por dois discos tem de custar duas enumerações, e não dez. O
        agrupamento vive dentro desta chamada e morre com ela: lembrar uma
        resposta entre chamadas seria exatamente o cache que a porta proíbe.

        A ordem e o tamanho de `images` são mantidos exatamente, para que um
        chamador com uma lista ranqueada possa emparelhar as duas de volta sem
        casar ids -- o ranking é do repositório, e nada aqui pode reordená-lo.

        Uma imagem cujo dispositivo não tem linha é uma invariante quebrada,
        e não um pedido a recusar: `images.device_id` é uma chave estrangeira,
        então o PostgreSQL não consegue produzir uma. Levanta `LookupError`,
        que nenhum handler de erro traduz para um 4xx, porque um bug tem de
        continuar sendo um 500.
        """
        located_devices: dict[DeviceId, tuple[Device, Path | None]] = {}
        for device_id in dict.fromkeys(image.device_id for image in images):
            device = self._devices.get(device_id)
            if device is None:
                raise LookupError(
                    f"Device {device_id} has indexed images but no row; "
                    "images.device_id is a foreign key, so this should be "
                    "impossible."
                )
            located_devices[device_id] = (device, self._locator.mount_point(device))

        located: list[LocatedImage] = []
        for image in images:
            device, mount_point = located_devices[image.device_id]
            located.append(
                LocatedImage(image=_with_location(image, mount_point), device=device)
            )
        return located

    def execute_one(self, image: Image) -> LocatedImage:
        """Localiza uma única imagem; o caso de um elemento de `execute()`."""
        (located,) = self.execute([image])
        return located


def _with_location(image: Image, mount_point: Path | None) -> Image:
    """Devolve `image` com o caminho absoluto para `mount_point`, ou sem nenhum.

    O `None` é escrito de forma explícita, em vez de deixar o que a entidade
    trazia. As linhas voltam do repositório sem caminho, mas um `Image`
    construído pelo worker de indexação tem um, e uma resolução que mantivesse
    um caminho velho para um disco desplugado o relataria como conectado.
    """
    absolute_path = (
        ImagePath(mount_point / str(image.relative_path))
        if mount_point is not None
        else None
    )
    return dataclasses.replace(image, absolute_path=absolute_path)
