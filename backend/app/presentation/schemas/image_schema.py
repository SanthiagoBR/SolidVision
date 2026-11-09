"""Formato da resposta de uma imagem indexada, e o disco em que ela está (RFC-030 §4).

O RFC-026 deixou este módulo como placeholder, e o placeholder tinha motivo:
publicar *onde* um arquivo está era uma questão em aberto. O RFC-030 a
respondeu.

**`relative_path` sempre, `absolute_path` quando o disco está aqui.** O
primeiro é um fato guardado, que vale esteja ou não algo plugado, e sozinho já
diz ao usuário quase tudo o que ele quer -- "fotos/2018/junho". O segundo é
derivado por requisição de um ponto de montagem resolvido naquele momento, e é
`null` exatamente quando não há de onde derivá-lo.

**Nenhum dos dois é identificador.** `id` é; os caminhos servem para mostrar e
para o servidor agir. Nenhuma rota aceita um caminho de volta, e um cliente
que guarde um para enviar depois não tem para onde enviá-lo (RFC-030 §5.1).

O RFC-026 deu dois argumentos contra publicar o caminho. O primeiro -- vazar
a estrutura do sistema de arquivos do servidor -- não vale para uma
implantação local-first, em que o sistema de arquivos do servidor é o do
próprio usuário (RFC-030 §2.1). O segundo -- um caminho é um identificador
instável -- continua valendo, e é por isso que os caminhos estão aqui só para
exibição.
"""

from __future__ import annotations

import datetime
from typing import TypedDict
from uuid import UUID

from pydantic import BaseModel, Field

from app.application.use_cases.resolve_image_location import LocatedImage
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.position_source import PositionSource


class DeviceSchema(BaseModel):
    """O disco da imagem, com o nome que o usuário lhe deu, e se ele está aqui."""

    id: UUID = Field(description="Stable identifier of the device")
    label: str = Field(description="The user's name for the disk, e.g. HD3")
    connected: bool = Field(
        description=(
            "Whether the disk was mounted when this response was built. "
            "Asked of the operating system per request and never stored, so "
            "it can change between two requests for the same image."
        )
    )


class ImageSchema(BaseModel):
    """Uma imagem indexada, onde ela está, e se dá para alcançá-la agora."""

    id: UUID = Field(description="Stable identifier of the image")
    filename: str = Field(description="Name of the file, without its extension")
    captured_at: datetime.datetime | None = Field(
        description=(
            "When the photo was taken, in the camera's local time, with no "
            "time zone and no offset (e.g. 2018-07-14T15:32:05). Null when "
            "unknown."
        )
    )
    capture_source: CaptureSource | None = Field(
        description=(
            "Where captured_at came from: exif_original (the camera clock at "
            "the shot), exif_digitized (when the image was digitised), or "
            "unknown (the file carries no date). Null if the file has not "
            "been examined yet."
        )
    )
    latitude: float | None = Field(
        description=(
            "Where the photo was taken, north-south, in decimal degrees "
            "(WGS 84; negative is south). This is where the drone was, not "
            "the ground it photographed, which for an oblique shot can be "
            "tens to hundreds of metres away. Null when unknown; latitude "
            "and longitude are always both present or both null."
        )
    )
    longitude: float | None = Field(
        description=(
            "Where the photo was taken, east-west, in decimal degrees "
            "(negative is west). Null exactly when latitude is."
        )
    )
    position_source: PositionSource | None = Field(
        description=(
            "Where latitude and longitude came from: exif_gps (the GPS block "
            "of the file's EXIF) or unknown (the file has no usable position). "
            "Null if the file has not been examined yet."
        )
    )
    device: DeviceSchema = Field(description="The disk the file is on")
    relative_path: str = Field(
        description=(
            "Where the file is within its disk, with forward slashes, e.g. "
            "fotos/2018/junho/DJI_0042.JPG. Always present, whether or not "
            "the disk is connected. For display: it is not an identifier and "
            "no route accepts it."
        )
    )
    absolute_path: str | None = Field(
        description=(
            "Where the file is on this machine right now, e.g. "
            "F:/fotos/2018/junho/DJI_0042.JPG. Null when the disk is not "
            "connected -- which is an answer, not an error: the file is on "
            "device.label. For display: it is not an identifier and no route "
            "accepts it."
        )
    )

    @classmethod
    def from_located(cls, located: LocatedImage) -> ImageSchema:
        """Monta uma imagem localizada para a resposta, sem acrescentar nada.

        Os caminhos saem por `ImagePath.__str__`, que é no estilo POSIX em
        toda plataforma. `str(Path)` no Windows publicaria barras invertidas,
        e um cliente JSON teria então de saber qual sistema operacional
        respondeu para dividir um caminho.
        """
        return cls(**image_fields(located))


class ImageFields(TypedDict):
    """Os argumentos nomeados de `ImageSchema`, tipados para o `**` ser checado."""

    id: UUID
    filename: str
    captured_at: datetime.datetime | None
    capture_source: CaptureSource | None
    latitude: float | None
    longitude: float | None
    position_source: PositionSource | None
    device: DeviceSchema
    relative_path: str
    absolute_path: str | None


def image_fields(located: LocatedImage) -> ImageFields:
    """Os campos que toda resposta em forma de imagem compartilha, num lugar só.

    Compartilhados com `search_schema.py`, cujos resultados são este formato
    mais uma pontuação, para que as duas respostas não derivem para descrever
    uma imagem de dois jeitos. É também por isso que a posição (RFC-032) está
    aqui, e não só no resultado da busca: uma foto achada por "perto daqui" e
    a mesma foto aberta pelo id precisam informar as mesmas coordenadas.
    """
    image = located.image
    return {
        "id": image.id.value,
        "filename": image.filename,
        "captured_at": image.captured_at,
        "capture_source": image.capture_source,
        "latitude": image.latitude,
        "longitude": image.longitude,
        "position_source": image.position_source,
        "device": DeviceSchema(
            id=located.device.id.value,
            label=located.device.label,
            connected=located.connected,
        ),
        "relative_path": str(image.relative_path),
        "absolute_path": (
            str(image.absolute_path) if image.absolute_path is not None else None
        ),
    }
