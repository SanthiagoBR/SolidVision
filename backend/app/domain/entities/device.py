"""Entidade Device da camada de domínio (RFC-027).

Um dispositivo é um volume físico: um disco externo, um cartão de memória, o
disco interno. Ele *não* é uma `Collection` -- um disco guarda várias pastas
indexadas, indexadas em momentos diferentes contra versões diferentes do
modelo, e fundir os dois obrigaria um disco inteiro a compartilhar uma versão
só do modelo de embedding (RFC-027 §10).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from app.domain.value_objects.device_id import DeviceId, VolumeIdentity


@dataclass(frozen=True)
class Device:
    """Entidade de negócio imutável que representa um volume de armazenamento.

    Congelada e comparada pelo `id`, seguindo `Image` (RFC-009): dois valores
    que descrevem o mesmo disco em dois momentos -- um `last_seen_at`
    diferente, um rótulo renomeado -- são o mesmo dispositivo, e um conjunto de
    dispositivos não pode contê-lo duas vezes.

    **Não há campo `mount_point`, nem campo `drive_letter`.** Persistir
    qualquer um deles reintroduziria justamente o dado instável que esta
    entidade existe para eliminar: no Windows, a letra de um volume removível
    é função da ordem de montagem, então o mesmo disco é `D:` hoje e `F:`
    amanhã sem que nada nele tenha mudado (RFC-027 §2.1). Onde o volume está
    montado *agora* é resolvido quando perguntado, por
    `VolumeIdentityProvider.mounted_volumes()`, e nunca é anotado.

    Pela mesma razão, não há campo `is_connected`. Ele estaria errado em toda
    leitura feita depois de o usuário desplugar o disco, e nada jamais o
    corrigiria -- o Windows não avisa este processo (RFC-027 §7).
    """

    id: DeviceId
    volume_identity: VolumeIdentity
    label: str
    """O nome que o usuário dá ao disco -- "HD2".

    Separado de `filesystem_label` porque esse muitas vezes é `Untitled`, ou
    vazio, ou a string de marketing do fabricante. Um produto que promete
    dizer *"está no HD3"* precisa de um lugar para guardar "HD3".
    """

    filesystem_label: str | None = None
    """O que o próprio volume informa. Informativo, nunca um identificador."""

    total_bytes: int | None = None
    first_seen_at: datetime.datetime | None = None
    last_seen_at: datetime.datetime | None = None
    """Quando este disco foi visto plugado pela última vez -- histórico, não estado.

    A distinção é a que o RFC-027 §7 faz: isto responde "quando o vi pela
    última vez", que é um fato sobre o passado e continua verdadeiro. Nunca
    responde "ele está plugado agora", que é um fato sobre o presente que
    nenhum valor guardado consegue manter.
    """

    last_scan_at: datetime.datetime | None = None
    last_scan_file_count: int | None = None
    """Quantos arquivos suportados o último scan achou neste volume.

    O denominador do número de "% indexado", persistido separado do
    resultado da indexação porque os dois respondem a perguntas diferentes
    (RFC-027 §8). Um scan é barato -- `stat`, sem inferência -- e, sem uma
    contagem própria, a porcentagem só poderia ser calculada contra as linhas
    que já estão em `images`, o que chega a 100% depois de qualquer execução
    completa do pipeline e nunca vê um arquivo que nunca passou pelo scan.

    Tudo o que desenha uma porcentagem a partir disto tem de desenhar também
    *quando* o scan aconteceu. "50%" sozinho é um número errado e confiante
    para um usuário que copiou 10.000 fotos para o disco ontem.
    """

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Device):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
