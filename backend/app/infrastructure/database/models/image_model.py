"""Modelo SQLAlchemy para persistir os metadados de imagens."""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.image import Image
from app.domain.value_objects.capture_source import CaptureSource
from app.domain.value_objects.device_id import DeviceId
from app.domain.value_objects.image_id import ImageId
from app.domain.value_objects.image_path import ImagePath
from app.domain.value_objects.position_source import PositionSource
from app.infrastructure.persistence.base import Base

SHA256_HEX_LENGTH = 64

# A largura da projeção do checkpoint CLIP do RFC-023 e, portanto, a largura
# da coluna `vector` que a migration do RFC-023 criou. Nomeada aqui em vez de
# repetida como literal porque a busca tem de validar os vetores de consulta
# contra o mesmo número, e os dois não podem ter como divergir.
EMBEDDING_DIMENSION = 512


class ImageModel(Base):
    """Representação SQLAlchemy de uma entidade de domínio de imagem."""

    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("devices.id", name="fk_images_device_id"),
        nullable=False,
    )
    """Em qual volume este arquivo mora.

    A metade da localização que não se move. O RFC-027 trocou a coluna
    absoluta `path` por esta mais `relative_path`, porque um caminho absoluto
    no Windows começa com uma letra de unidade, e a letra de unidade é
    atribuída pela ordem de montagem -- então o mesmo arquivo intocado mudava
    de localização registrada, e portanto de id `uuid5`, sempre que o disco
    calhava de montar como `F:` em vez de `D:` (RFC-027 §2.1).
    """

    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    """Onde o arquivo fica dentro do seu dispositivo, ex.: `fotos/2018/DJI_0042.JPG`.

    A outra metade. De propósito, não há coluna de caminho absoluto ao lado:
    manter as duas formas convidaria uma delas a ficar desatualizada, e a
    absoluta é justamente a que não dá para manter correta, já que muda sem
    ninguém escrever nada.
    """

    filename: Mapped[str] = mapped_column(String, nullable=False)
    extension: Mapped[str] = mapped_column(String, nullable=False)
    # Uma constante do módulo em vez de `settings.embedding_dimension` porque
    # isto é schema físico, fixado por uma migration: não pode seguir em
    # silêncio uma variável de ambiente de runtime para longe do que o banco
    # de fato guarda.
    # `test_image_model_embedding_column_matches_configured_dimension` afirma
    # que os dois continuam de acordo.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSION), nullable=True
    )
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_modified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Um digest SHA-256 em hexadecimal tem exatamente 64 caracteres, então o
    # comprimento é schema físico, não um palpite. Anulável porque o RFC-024
    # acrescentou a coluna sem backfill: linhas indexadas antes dele voltam
    # como NULL, o que a decisão de pular trata como "desconhecido", nunca
    # como "confere" (ver `plan_indexing`).
    content_hash: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH), nullable=True
    )

    captured_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    """Quando a fotografia foi tirada, como o relógio da câmera marcava (RFC-028).

    **`timezone=False`, duas colunas abaixo de um `file_modified_at` que é
    `timezone=True` -- e a diferença é o desenho, não uma inconsistência a
    arrumar.** `file_modified_at` vem de `st_mtime`, um instante absoluto, então
    pertence a `TIMESTAMPTZ`. Esta coluna vem do `DateTimeOriginal` do EXIF,
    que é uma leitura de relógio de parede local, sem fuso. Pô-la em
    `TIMESTAMPTZ` obrigaria a inventar um fuso -- UTC, ou o fuso da máquina
    que rodou o scan --, e qualquer um dos dois leva uma foto tirada às 22:00
    de 31 de dezembro para o ano seguinte para quem a lê em outro lugar
    (RFC-028 §5). "Fotos de 2018" é perguntado no horário local da câmera e é
    respondido aqui sem conversão nenhuma.

    O custo aceito: fotos de dois fusos diferentes não se ordenam estritamente
    entre si.
    """

    capture_source: Mapped[str | None] = mapped_column(String, nullable=True)
    """De onde veio o `captured_at` -- um valor de `CaptureSource`, ou NULL.

    Três estados, e os dois primeiros nunca podem ser fundidos:

    - NULL: a linha **nunca foi examinada** -- é anterior ao RFC-028, ou o
      disco dela foi varrido com a extração desligada;
    - `'unknown'`: examinada, e o arquivo não traz data utilizável, então
      `captured_at` é NULL;
    - `'exif_original'` / `'exif_digitized'`: a tag EXIF de onde a data foi
      lida.

    A separação entre NULL e `'unknown'` é o que mantém barato varrer de novo.
    Um scan escreve a data de captura só para as linhas ainda NULL, então,
    depois da primeira passada, uma coleção sem mudanças não custa escrita
    nenhuma. Se "examinada, sem data" também fosse guardado como NULL, todo
    scan releria e reescreveria cada arquivo sem data para sempre -- e
    arquivos sem data são o tipo mais numeroso justamente nas coleções que
    preocupam o RFC-028.

    Uma `String` simples, não um `ENUM` do PostgreSQL, para que acrescentar
    uma fonte não precise de migration.
    """

    thumbnail_path: Mapped[str | None] = mapped_column(String, nullable=True)
    """Onde fica a thumbnail desta imagem, relativo ao cache de thumbnails (RFC-030).

    NULL quer dizer *sem thumbnail*: a linha é anterior ao RFC-030, ou a
    renderização falhou, ou os bytes mudaram e a thumbnail nova ainda não foi
    feita. O `GET /images/{id}/thumbnail` responde 404 nos três casos e a UI
    mostra um placeholder (RFC-030 §7.2).

    **Relativo a `settings.thumbnail_directory`, nunca absoluto** -- o
    argumento que o RFC-027 fez para `relative_path`, um diretório adiante.
    Uma localização absoluta gravaria o diretório atual do cache em toda
    linha, então mover o cache deixaria cada uma apontando para o nada.

    **Esta coluna é o motivo de a reescrita barata da chave primária acabar
    aqui** (RFC-030 §7.3). O valor é derivado de `images.id`, e o nome do
    arquivo no disco também; derivar o id de novo depois deste ponto
    significa renomear arquivos além de atualizar linhas. Registrado como
    consequência assumida, como o RFC-029 §11 antecipou.
    """

    latitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    """Posição norte-sul onde a foto foi tirada, em graus decimais (RFC-032).

    **`double precision`, não `NUMERIC`.** 1e-7 grau é cerca de um centímetro
    e um `float8` carrega ~15 dígitos significativos: o que se perde está
    ordens de grandeza abaixo do erro do GNSS de um drone, e muitas mais
    abaixo do deslocamento entre aeronave e alvo que o raio da busca absorve
    (RFC-032 §5). `NUMERIC` compraria aritmética decimal para guardar ruído.

    **Duas colunas, não um `point`.** A entidade tem dois campos, a resposta
    publica dois números, e o CHECK de pareamento abaixo diz em SQL o que o
    `validate_position_fields()` diz em Python. Um `point` transformaria todo
    `latitude` em toda consulta, log e migration em `(images.position)[1]`.

    Nunca chamada de `location`: neste código essa palavra é onde o *arquivo*
    está (RFC-032 §4.5).
    """

    longitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    """Posição leste-oeste onde a foto foi tirada, em graus decimais; ver `latitude`."""

    position_source: Mapped[str | None] = mapped_column(String, nullable=True)
    """De onde veio a posição -- um valor de `PositionSource`, ou NULL.

    Três estados, como o `capture_source` tem (RFC-032 §4.4):

    - NULL: **nunca examinada** -- anterior ao RFC-032, ou varrida com
      `extract_gps` desligado;
    - `'unknown'`: examinada, e o arquivo não tem posição utilizável, então
      `latitude` e `longitude` são NULL;
    - `'exif_gps'`: lida do GPS IFD.

    Uma `String` simples, não um `ENUM`, para que `'manual'` e
    `'subject_estimated'` -- nomeados, e de propósito ainda não membros --
    não custem migration quando chegarem.
    """

    missing_since: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Desde quando um scan do disco inteiro não lista o arquivo, ou NULL (RFC-046).

    NULL é **presente**. Uma data é *"fora do scan desta data"*: a
    linha, o vetor e a thumbnail ficam, toda leitura que serve a interface a
    esconde, e o próximo scan que listar o arquivo de novo a volta para NULL --
    sem inferência, porque o tamanho e a data são os que a linha guarda
    (RFC-046 §3).

    **Uma data e não um booleano**, por dois motivos (§5.1): o cartão diz
    desde quando -- *"não encontradas no scan de 12/03"* -- e uma purga por
    idade, se um dia existir, não precisa de outra migration.

    `TIMESTAMPTZ`, como `file_modified_at`: é um instante no relógio desta
    máquina, não a leitura do relógio de parede de uma câmera. Sem índice: o
    filtro descarta quase nada (§6).
    """

    __table_args__ = (
        UniqueConstraint(
            "device_id", "relative_path", name="uq_images_device_relative_path"
        ),
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name="position_pairing",
        ),
        CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name="latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name="longitude_range",
        ),
    )
    """Uma linha por arquivo por dispositivo, e nunca meia posição.

    O par único substitui o antigo `UNIQUE (path)`, que parecia impedir
    duplicatas e não impedia nada: `D:/fotos/x.JPG` e `F:/fotos/x.JPG` são
    duas strings diferentes, então o mesmo arquivo no mesmo disco indexado
    sob duas letras de unidade satisfazia a restrição duas vezes. O par de
    fato restringe o que diz restringir.

    Os três CHECKs são a parte do invariante do RFC-032 que nenhum escritor
    futuro consegue contornar -- um `UPDATE` manual, um backfill descuidado,
    uma fixture de teste montada à mão. O domínio ainda valida primeiro e
    produz a mensagem legível; estes fazem o banco recusar o que nunca foi
    perguntado ao domínio. Declarados aqui e também na migration para que um
    schema montado a partir dos modelos (`Base.metadata.create_all`) os tenha
    também. Os nomes são a forma curta porque a convenção de nomes em
    `base.py` prefixa `ck_images_`: o banco guarda
    `ck_images_position_pairing`, `ck_images_latitude_range` e
    `ck_images_longitude_range`.
    """

    @classmethod
    def from_domain(cls, image: Image) -> ImageModel:
        """Cria um modelo ORM a partir de uma entidade de domínio.

        A entidade de domínio `Image` não tem campos de embedding nem de
        metadados do sistema de arquivos, então essas colunas exclusivas da
        persistência ficam sem valor aqui; elas são preenchidas à parte pelo
        pipeline de indexação quando ele existir.

        A data de captura *é* copiada, porque desde o RFC-028 ela é um campo
        da entidade: um atributo da fotografia, e não um sinal de mudança
        para o pipeline. A posição também, desde o RFC-032, pelo mesmo
        motivo.
        """
        return cls(
            id=image.id.value,
            device_id=image.device_id.value,
            relative_path=str(image.relative_path),
            filename=image.filename,
            extension=image.extension,
            embedding=None,
            file_size=None,
            file_modified_at=None,
            content_hash=None,
            captured_at=image.captured_at,
            capture_source=(
                image.capture_source.value if image.capture_source else None
            ),
            latitude=image.latitude,
            longitude=image.longitude,
            position_source=(
                image.position_source.value if image.position_source else None
            ),
            thumbnail_path=None,
            missing_since=None,
        )

    def to_domain(self) -> Image:
        """Cria uma entidade de domínio a partir deste modelo ORM.

        `absolute_path` fica `None`, e isso não é uma omissão. A linha
        registra onde o arquivo está no seu *dispositivo*; onde esse
        dispositivo está montado agora não está no banco, de propósito, e não
        dá para saber a partir de uma linha (RFC-027 §7). Quem precisa de um
        caminho utilizável resolve o ponto de montagem e o preenche -- o
        worker de indexação enquanto varre, e o RFC-030 por requisição.
        """
        return Image(
            id=ImageId(self.id),
            device_id=DeviceId(self.device_id),
            relative_path=ImagePath(self.relative_path),
            filename=self.filename,
            extension=self.extension,
            captured_at=self.captured_at,
            capture_source=(
                CaptureSource(self.capture_source) if self.capture_source else None
            ),
            latitude=self.latitude,
            longitude=self.longitude,
            position_source=(
                PositionSource(self.position_source) if self.position_source else None
            ),
        )
