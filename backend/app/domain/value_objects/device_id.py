"""Value objects da identidade de dispositivo para a camada de domínio (RFC-027).

Dois value objects, e a distinção entre eles é todo o ponto deste RFC.
`VolumeIdentity` é *o que o sistema operacional informa* sobre um volume --
uma string opaca que a plataforma garante ser estável entre remontagens,
reinicializações e portas. `DeviceId` é como o SolidVision chama esse volume
internamente, derivado da identidade para que o mesmo disco sempre caia na
mesma linha.

Nenhum dos dois é uma letra de unidade, e nenhum pode jamais virar uma. Uma
letra é função da ordem de montagem, e não do volume (RFC-027 §2.1), e
gravar uma é o que faz o caminho de um arquivo parado mudar sozinho.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from uuid import UUID

from app.domain.exceptions import (
    InvalidDeviceIdentifierError,
    InvalidVolumeIdentityError,
)


class VolumeKind(enum.Enum):
    """De qual esquema de identificadores de plataforma veio uma `VolumeIdentity`.

    O discriminador existe para que o segundo adaptador não exija uma
    migration (RFC-027 §4.1). Só `WINDOWS_VOLUME_GUID` tem adaptador hoje; os
    outros dois membros estão declarados e não implementados de propósito,
    porque um adaptador Linux que ninguém rodou seria uma promessa de
    portabilidade que ninguém verificou.

    É também o que mantém honesta a restrição `UNIQUE` sobre
    `volume_identity`: duas plataformas são livres para criar strings que
    parecem idênticas, e o tipo é o que diz que elas não são o mesmo volume.
    """

    WINDOWS_VOLUME_GUID = "windows-volume-guid"
    r"""`\\?\Volume{GUID}\`, de `GetVolumeNameForVolumeMountPoint`."""

    LINUX_FS_UUID = "linux-fs-uuid"
    """O UUID do sistema de arquivos em `/dev/disk/by-uuid`. Sem adaptador ainda."""

    MACOS_VOLUME_UUID = "macos-volume-uuid"
    """O UUID do volume informado pelo `diskutil`. Sem adaptador ainda."""


@dataclass(frozen=True)
class VolumeIdentity:
    """Um identificador de um volume físico, estável na plataforma.

    `value` é opaco de propósito: nada acima do adaptador que o produziu pode
    interpretá-lo, compará-lo sem diferenciar maiúsculas de minúsculas ou
    montar um caminho a partir dele. É uma chave, e as únicas operações sobre
    uma chave são igualdade e hash.

    Estável até o volume ser reformatado, que é a limitação aceita (RFC-027
    §4.1): reformatar destrói as fotos, então perder as linhas é a resposta
    certa, e não um efeito colateral.
    """

    value: str
    kind: VolumeKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VolumeKind):
            raise InvalidVolumeIdentityError(
                "Volume identity kind must be a VolumeKind member"
            )
        if not isinstance(self.value, str) or not self.value.strip():
            raise InvalidVolumeIdentityError("Volume identity cannot be empty")

    def __str__(self) -> str:
        """Escreve como `kind:value`, a forma de que a derivação do id tira o hash.

        As duas metades entram para que duas plataformas que informam a mesma
        string opaca continuem produzindo dois dispositivos diferentes.
        """
        return f"{self.kind.value}:{self.value}"


@dataclass(frozen=True)
class DeviceId:
    """Identificador fortemente tipado de um dispositivo.

    Modelado em `ImageId` até na normalização, porque os dois são usados do
    mesmo jeito: um UUID que um repositório guarda como UUID, embrulhado para
    que um `uuid.UUID` solto não possa ser passado onde se queria um
    dispositivo.
    """

    value: UUID

    def __init__(self, value: UUID | str) -> None:
        normalized_value = self._normalize(value)
        object.__setattr__(self, "value", normalized_value)

    @staticmethod
    def _normalize(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value

        if isinstance(value, str):
            if not value.strip():
                raise InvalidDeviceIdentifierError("Device identifier cannot be empty")

            try:
                return UUID(value)
            except ValueError as exc:
                raise InvalidDeviceIdentifierError(
                    "Device identifier must be a valid UUID"
                ) from exc

        raise InvalidDeviceIdentifierError("Device identifier must be a UUID or string")

    def __str__(self) -> str:
        """Escreve como a string do UUID, sem mais nada.

        Essencial, e não cosmético: `compute_image_id()` tira o hash de
        `f"{device_id}/{relative_path}"`, então isto é metade da identidade
        de toda imagem do sistema. Mudá-lo mudaria todos os ids.
        """
        return str(self.value)
