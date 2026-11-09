"""Derivação determinística da identidade dos dispositivos de armazenamento (RFC-027).

Um `DeviceId` é um `uuid5` sobre algo que a plataforma informa, então plugar
o mesmo disco numa segunda máquina, ou depois de reiniciar, ou em outra
porta, cai na mesma linha sem que nada precise ser procurado antes.

**Isto mora no Domain desde o RFC-031, e antes morava em
`app/infrastructure/filesystem/device_identity.py`.** Nada da derivação
jamais foi específico de plataforma: ela calcula o hash de um
`VolumeIdentity`, que é um value object do Domain, e produz um `DeviceId`,
que é outro. Ficava na Infrastructure só porque o único chamador dela
ficava. O RFC-031 lhe deu um segundo chamador na camada Application --
`RegisterDeviceUseCase`, que cria o id de um disco registrado por HTTP -- e
`test_application_architecture.py` proíbe um caso de uso de importar
`app.infrastructure`, então a função mudou para a camada a que sempre
pertenceu, em vez de a regra ser torcida em volta dela.

**O UUID de namespace veio junto, byte a byte.** Ele foi gerado uma vez com
`uuid.uuid4()` e está congelado para sempre: gerá-lo de novo criaria um
segundo `DeviceId` para cada disco já conhecido, deixando órfã toda linha de
imagem que aponta para o primeiro -- o `ImageId` é `uuid5` sobre
`f"{device_id}/{relative_path}"`, então ele é metade da identidade de toda
imagem do sistema. Nunca o gere de novo.

`compute_image_id()` de propósito **não** mudou de lugar. Ele não é
necessário acima da Infrastructure, e uma refatoração gratuita "por
simetria" seria uma refatoração da identidade de toda imagem do banco.
"""

from __future__ import annotations

import uuid

from app.domain.value_objects.device_id import DeviceId, VolumeIdentity

SOLIDVISION_VOLUME_NAMESPACE = uuid.UUID("6a1f4c07-9f0c-4a2e-9d84-0f3f0d5c4b91")


def compute_device_id(volume_identity: VolumeIdentity) -> DeviceId:
    """Deriva um `DeviceId` determinístico da identidade estável de um volume.

    Calcula o hash de `str(volume_identity)`, que é `kind:value` -- as duas
    metades, para que duas plataformas que informam a mesma string opaca
    continuem descrevendo dois dispositivos diferentes. É a mesma razão de
    `VolumeKind` existir: o discriminador precisa fazer parte da chave, e não
    ser um comentário ao lado dela.

    Reformatar o volume muda o que a plataforma informa e, portanto, muda o
    id. Aceito sem tratamento especial: uma formatação destrói as fotos,
    então deixar o dispositivo delas órfão é a resposta certa, e não um
    defeito (RFC-027 §4.1).
    """
    return DeviceId(uuid.uuid5(SOLIDVISION_VOLUME_NAMESPACE, str(volume_identity)))
