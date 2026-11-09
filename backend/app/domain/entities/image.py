"""Entidade Image da camada de domínio."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.exceptions import DeviceNotConnectedError
from app.domain.value_objects.capture_date import CaptureDate, validate_capture_fields
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position import (
    Position,
    PositionReading,
    validate_position_fields,
)
from app.domain.value_objects.position_source import PositionSource


@dataclass(frozen=True)
class Image:
    """Entidade de negócio imutável que representa uma imagem que o sistema conhece.

    O RFC-027 trocou o `path` absoluto único pelo par
    `(device_id, relative_path)`. O caminho absoluto não descrevia onde o
    arquivo está; descrevia onde o arquivo estava *da última vez que alguém
    olhou*, porque a letra de um volume removível é atribuída pela ordem de
    montagem. Guardá-lo fazia o local registrado de um arquivo parado mudar
    sozinho, levando junto o `ImageId` derivado (RFC-027 §2.1).

    O par não tem esse problema. Um id de dispositivo é derivado de um
    identificador que o sistema operacional mantém estável entre montagens, e
    um caminho relativo ao ponto de montagem do dispositivo não contém a
    parte que se move.
    """

    id: ImageId
    device_id: DeviceId
    relative_path: ImagePath
    """Onde o arquivo fica *dentro do dispositivo*, p. ex. `fotos/2018/DJI_0042.JPG`.

    Nunca começa com letra de unidade nem ponto de montagem. Juntá-lo a um
    ponto de montagem resolvido no momento do uso é como se obtém um caminho
    absoluto -- ver `absolute_path`, abaixo.
    """

    filename: str
    extension: str

    absolute_path: ImagePath | None = None
    """Onde o arquivo está agora, quando isso se pode saber no momento.

    Calculado, nunca persistido e, portanto, nunca parte do que torna duas
    imagens iguais. `None` é uma resposta de primeira classe, com um sentido
    preciso: *o dispositivo desta imagem não está montado, então ela não tem
    caminho absoluto neste instante.* É exatamente o caso que o RFC-027 §2.3
    existe para representar -- um resultado de busca num disco na gaveta é
    uma resposta útil ("está no HD3"), e não uma quebrada, e seria mentira
    entregar a ele um caminho que não resolve para nada.

    Preenchido por quem resolveu o ponto de montagem: o worker de indexação,
    que descobriu o arquivo num disco conectado, e, depois, o RFC-030, que o
    resolve a cada pedido para a resposta da busca. Um componente que precisa
    dos bytes -- o hasher do conteúdo, o adaptador CLIP -- lê este campo e
    tem de falhar de forma ruidosa quando ele é `None`, em vez de reconstruir
    um caminho próprio a partir do relativo.
    """

    captured_at: datetime.datetime | None = None
    """Quando a fotografia foi tirada, segundo o relógio da câmera.

    Sem fuso, sempre: o EXIF registra a hora local do relógio, sem fuso, e
    este campo a mantém assim da extração até a resposta HTTP (RFC-028 §5).
    `None` quer dizer desconhecida -- o arquivo nunca foi examinado, ou foi
    examinado e não tinha data --, e uma imagem com data desconhecida nunca
    casa com um filtro de intervalo de datas (RFC-028 §4.1).

    Aqui na entidade, ao contrário de `file_size`, `file_modified_at` e
    `content_hash`, e a diferença é *para que* serve cada um. Aqueles três são
    sinais de mudança: só a decisão de pular, incremental, os lê, então eles
    viajam em `IndexMetadata` e `IndexingRecord`. Uma data de captura é um
    atributo buscável da fotografia. Ela tem de chegar à resposta da busca
    por `SearchHit`, e tem de ser visível ao predicado do filtro que os
    repositórios em memória avaliam contra um `Image`. Mantê-la fora da
    entidade obrigaria esses dublês a carregar um dicionário à parte só para
    filtrar -- uma segunda representação do mesmo fato, que é a divergência
    que o teste de contrato compartilhado existe para impedir.

    O mesmo argumento que `SearchHit` usa para manter `similarity` *fora*
    desta entidade corta para o outro lado aqui: uma similaridade não quer
    dizer nada sem a sua consulta, enquanto uma data de captura é a mesma
    para todo chamador.

    Fora da igualdade, que continua pelo `id`, como todos os outros campos.
    """

    capture_source: CaptureSource | None = None
    """De onde veio `captured_at`; `None` se o arquivo nunca foi examinado.

    `None` e `CaptureSource.UNKNOWN` são respostas diferentes, e o scan
    depende da diferença: as linhas `None` têm a data escrita no próximo scan
    que as vê; as `UNKNOWN` ficam como estão, porque reler um arquivo que não
    tinha data não dá data nenhuma (RFC-028 §4.2).
    """

    latitude: float | None = None
    """Onde a fotografia foi tirada, norte-sul, em graus decimais (RFC-032).

    Em par com `longitude`: as duas ou nenhuma, nunca meia posição (RFC-032
    §4.3). `None` quer dizer desconhecida -- nunca examinada, ou examinada
    sem um fix de GPS utilizável --, e uma imagem de posição desconhecida
    nunca casa com um círculo de busca (RFC-020).

    **A posição da aeronave, e não a do assunto.** Numa foto oblíqua, o chão
    fotografado pode estar a dezenas ou centenas de metros (RFC-032 §2.2), e
    nada aqui corrige isso.

    Na entidade, ao lado de `captured_at`, pela razão que aquele campo dá:
    uma posição é um atributo buscável da fotografia, chega à resposta por
    `SearchHit`, e o predicado do filtro em memória a lê do `Image`. Nunca
    chamada `location`, que neste código é onde o *arquivo* está (RFC-032
    §4.5).

    Fora da igualdade, que continua pelo `id`, como todos os outros campos.
    """

    longitude: float | None = None
    """Onde a fotografia foi tirada, leste-oeste, em graus decimais; ver `latitude`."""

    position_source: PositionSource | None = None
    """De onde veio a posição; `None` se o arquivo nunca foi examinado.

    `None` e `PositionSource.UNKNOWN` são respostas diferentes, pela razão
    que `capture_source` dá: as linhas `None` têm a posição escrita no
    próximo scan que as vê; as `UNKNOWN` ficam como estão (RFC-032 §4.4).
    """

    def __post_init__(self) -> None:
        validate_capture_fields(self.captured_at, self.capture_source)
        validate_position_fields(self.latitude, self.longitude, self.position_source)

    @property
    def capture_date(self) -> CaptureDate | None:
        """A data de captura examinada como um valor, ou `None` se nunca examinada."""
        if self.capture_source is None:
            return None
        return CaptureDate(captured_at=self.captured_at, source=self.capture_source)

    @property
    def position(self) -> Position | None:
        """O ponto, quando conhecido; `None` se desconhecido ou nunca examinado.

        Para as perguntas que só se importam com o ponto -- está dentro de um
        círculo, em que célula do mapa está. Os chamadores que precisam
        distinguir "nunca examinado" de "examinado, nenhum" leem
        `position_reading`.
        """
        if self.latitude is None or self.longitude is None:
            return None
        return Position(self.latitude, self.longitude)

    @property
    def position_reading(self) -> PositionReading | None:
        """A posição examinada como um valor só, ou `None` se nunca examinada."""
        if self.position_source is None:
            return None
        return PositionReading(position=self.position, source=self.position_source)

    @property
    def display_path(self) -> ImagePath:
        """O caminho mais útil disponível, para uma linha de log ou relato de erro.

        O absoluto quando o dispositivo está montado; senão, o relativo ao
        dispositivo. Nunca levanta exceção, porque os chamadores são caminhos de
        erro: uma execução que já está relatando um arquivo que falhou não pode
        falhar de novo ao nomeá-lo.

        Distinto de `require_absolute_path()` de propósito. Este é para uma
        pessoa ler; aquele é para abrir um arquivo, e os dois não podem ser
        confundidos -- um componente que passasse `display_path` a `open()`
        leria, para um dispositivo não montado, um caminho relativo ao
        diretório de trabalho do processo.
        """
        return self.absolute_path or self.relative_path

    def require_absolute_path(self) -> ImagePath:
        """Devolve `absolute_path`; falha se o dispositivo não está montado.

        Um lugar só para falhar, em vez de um por adaptador que precisa de
        bytes. A alternativa -- `Sha256ContentHasher` e `ClipEmbeddingModel`
        fazendo cada um o seu `if is None` -- são duas chances de um deles, em
        vez disso, reconstruir um caminho a partir de `relative_path`, o que
        produziria um caminho relativo ao diretório de trabalho do processo: uma
        string de aparência plausível que lê o arquivo errado ou, pior, um sem
        relação nenhuma.

        Levanta `DeviceNotConnectedError`, e não `FileNotFoundError`, porque os
        dois significam coisas diferentes para um chamador (ver essa exceção).
        """
        if self.absolute_path is None:
            raise DeviceNotConnectedError(
                f"{self.relative_path} has no resolved location: device "
                f"{self.device_id} is not connected."
            )
        return self.absolute_path

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Image):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
