"""Modelo SQLAlchemy para persistir dispositivos de armazenamento (RFC-027)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity, VolumeKind
from app.infrastructure.persistence.base import Base


class DeviceModel(Base):
    """Representação SQLAlchemy de uma entidade de domínio `Device`.

    **Não há coluna `drive_letter` nem coluna `mount_point`**, e a ausência é
    o desenho. A letra de um volume removível é atribuída pela ordem de
    montagem, então persistir uma faria a localização registrada de um
    arquivo parado mudar sozinha -- que é o defeito que o RFC-027 existe para
    remover, não uma conveniência que ele esqueceu.

    **Também não há coluna `is_connected`.** Ela estaria errada em toda
    leitura feita depois que o usuário desplugasse o disco, e nada jamais a
    corrigiria, porque o Windows não avisa este processo (RFC-027 §7). A
    conexão é resolvida enumerando o que está montado no momento em que a
    pergunta é feita.
    """

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    volume_identity: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    """A string opaca que o sistema operacional informa para este volume.

    Única, porque duas linhas reivindicando o mesmo disco físico são
    exatamente a duplicação que o RFC-027 remove. Guardada como texto e
    nunca interpretada: é uma chave, e as únicas operações sobre uma chave
    são igualdade e hash.
    """

    volume_kind: Mapped[str] = mapped_column(String, nullable=False)
    """De qual esquema de plataforma veio o `volume_identity`.

    Persistido para que acrescentar o adaptador de Linux ou macOS não precise
    de migration (RFC-027 §4.1), e para que strings de aparência idêntica de
    duas plataformas continuem sendo dois dispositivos.
    """

    label: Mapped[str] = mapped_column(String, nullable=False)
    filesystem_label: Mapped[str | None] = mapped_column(String, nullable=True)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """Quando o disco foi visto plugado pela última vez -- histórico, não estado.

    Responde "quando o vi pela última vez", o que continua verdade para
    sempre. Nunca responde "está plugado agora", o que nenhum valor guardado
    consegue manter.
    """

    last_scan_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scan_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Quantos arquivos suportados o último scan achou neste volume.

    O denominador declarado do "% indexado" (RFC-027 §8). Mantido à parte de
    qualquer coisa em `images` porque os dois contam coisas diferentes: as
    linhas de `images` só podem descrever arquivos que alguém já varreu,
    então uma porcentagem calculada só a partir delas chega a 100% depois de
    qualquer execução completa e nunca vê um arquivo que nunca foi olhado.

    Anulável porque um dispositivo que nunca passou por scan não tem
    denominador, e um denominador ausente tem de aparecer como "sem scan",
    nunca como zero.
    """

    __table_args__ = (
        CheckConstraint("total_bytes >= 0", name="total_bytes_non_negative"),
        CheckConstraint(
            "last_scan_file_count >= 0", name="last_scan_file_count_non_negative"
        ),
    )

    @classmethod
    def from_domain(cls, device: Device) -> DeviceModel:
        """Cria um modelo ORM a partir de uma entidade de domínio.

        `first_seen_at` e `last_seen_at` são NOT NULL no schema, mas opcionais
        na entidade, então um dispositivo construído sem eles recebe "agora"
        aqui em vez de ser rejeitado. Este é o lugar certo para o default: os
        timestamps descrevem quando a persistência observou o disco, e a
        entidade de domínio fica utilizável em testes e em memória sem
        inventar um relógio.
        """
        seen_at = datetime.datetime.now(tz=datetime.UTC)
        return cls(
            id=device.id.value,
            volume_identity=device.volume_identity.value,
            volume_kind=device.volume_identity.kind.value,
            label=device.label,
            filesystem_label=device.filesystem_label,
            total_bytes=device.total_bytes,
            first_seen_at=device.first_seen_at or seen_at,
            last_seen_at=device.last_seen_at or seen_at,
            last_scan_at=device.last_scan_at,
            last_scan_file_count=device.last_scan_file_count,
        )

    def to_domain(self) -> Device:
        """Cria uma entidade de domínio a partir deste modelo ORM."""
        return Device(
            id=DeviceId(self.id),
            volume_identity=VolumeIdentity(
                value=self.volume_identity, kind=VolumeKind(self.volume_kind)
            ),
            label=self.label,
            filesystem_label=self.filesystem_label,
            total_bytes=self.total_bytes,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            last_scan_at=self.last_scan_at,
            last_scan_file_count=self.last_scan_file_count,
        )
