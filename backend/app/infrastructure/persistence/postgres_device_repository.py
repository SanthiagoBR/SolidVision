"""Implementação em PostgreSQL da porta do repositório de dispositivos (RFC-027)."""

from __future__ import annotations

import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.entities.device import Device
from app.domain.repositories.device_repository import DeviceRepository
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity
from app.infrastructure.database.models.device_model import DeviceModel


class PostgresDeviceRepository(DeviceRepository):
    """Implementação do repositório sobre o PostgreSQL, via SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, device: Device) -> None:
        """Cria a linha, ou atualiza a que já tem este id.

        Um upsert de verdade, porque quem chama, naturalmente, é um worker
        começando uma execução num disco que provavelmente já viu: o id
        deriva da identidade do volume, então a segunda execução no mesmo
        disco chega aqui com a mesma chave primária e precisa da linha
        atualizada, não recusada.

        `first_seen_at` é escrito no insert e nunca mais tocado -- é o único
        campo que responde a uma pergunta sobre o passado, e um upsert que o
        sobrescrevesse trocaria, em silêncio, "desde março" por "desde
        agora" a cada execução. O resto muda: o rótulo quando o usuário
        renomeia o disco, a capacidade quando ele é trocado, os contadores
        do scan depois de um scan, e `last_seen_at` sempre.

        O rollback na falha segue o `PostgresImageRepository`: a sessão é
        compartilhada com as escritas de imagens pelo resto da execução,
        então uma linha de dispositivo recusada não pode deixar a transação
        num estado de rollback pendente que faça falhar a *próxima* escrita,
        sem relação com ela.
        """
        try:
            existing = self._session.get(DeviceModel, device.id.value)
            if existing is None:
                self._session.add(DeviceModel.from_domain(device))
            else:
                self._apply(existing, device)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _apply(model: DeviceModel, device: Device) -> None:
        """Copia cada campo mutável de `device` para uma linha existente."""
        model.volume_identity = device.volume_identity.value
        model.volume_kind = device.volume_identity.kind.value
        model.label = device.label
        model.filesystem_label = device.filesystem_label
        model.total_bytes = device.total_bytes
        model.last_seen_at = device.last_seen_at or datetime.datetime.now(
            tz=datetime.UTC
        )
        model.last_scan_at = device.last_scan_at
        model.last_scan_file_count = device.last_scan_file_count

    def record_scan(
        self, device_id: DeviceId, file_count: int, scanned_at: datetime.datetime
    ) -> None:
        """Um `UPDATE` das duas colunas do scan, pela chave do id.

        Não o `save()` acima, que copia cada campo de um `Device` montado
        por quem chama: a cópia do executor é do começo do job, e poria de
        volta um rótulo renomeado depois (RFC-039 §4.1). Uma linha que não
        existe não casa com nada, e nada é escrito.

        A cópia da linha que a sessão tiver, se tiver, é atualizada pelo
        próprio comando (a sincronização padrão do SQLAlchemy): as sessões
        do executor não expiram no commit (`session.py`), e um `get()`
        depois deste responderia, sem isso, pela cópia carregada antes. O
        rollback na falha é o do `save()`, pela mesma sessão compartilhada.
        """
        try:
            self._session.execute(
                update(DeviceModel)
                .where(DeviceModel.id == device_id.value)
                .values(last_scan_file_count=file_count, last_scan_at=scanned_at)
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def get(self, device_id: DeviceId) -> Device | None:
        model = self._session.get(DeviceModel, device_id.value)
        return model.to_domain() if model is not None else None

    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        """Busca o dispositivo pelo que o sistema operacional acabou de informar.

        As duas metades da identidade estão no `WHERE`. A string sozinha é
        única no schema hoje, mas é o tipo que impede que identificadores de
        mesmo formato, de duas plataformas, sejam lidos como um só volume, e
        deixá-lo de fora aqui faria isso depender de uma constraint em vez
        da consulta.
        """
        statement = select(DeviceModel).where(
            DeviceModel.volume_identity == identity.value,
            DeviceModel.volume_kind == identity.kind.value,
        )
        model = self._session.execute(statement).scalar_one_or_none()
        return model.to_domain() if model is not None else None

    def list(self) -> list[Device]:
        statement = select(DeviceModel)
        models = self._session.execute(statement).scalars().all()
        return [model.to_domain() for model in models]

    def delete(self, device_id: DeviceId) -> None:
        """Apaga a linha; a chave estrangeira recusa enquanto houver imagens.

        Sem cascata, e a recusa é a funcionalidade. `images.device_id` é uma
        chave estrangeira simples, sem `ON DELETE`, então o PostgreSQL
        levanta erro em vez de remover linhas cujos embeddings custaram
        horas de inferência -- exatamente o que o RFC-027 §6.3 diz que nunca
        pode acontecer em silêncio.
        """
        model = self._session.get(DeviceModel, device_id.value)
        if model is None:
            return
        try:
            self._session.delete(model)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
