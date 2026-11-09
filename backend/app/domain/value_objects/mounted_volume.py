"""Um volume que o sistema operacional diz estar conectado agora (RFC-031 §5).

O que `ResolvedVolume` é, escrito do lado do Domain da fronteira. A
Application precisa responder *"o que está plugado nesta máquina, e o que
disso já conhecemos?"*, e `ResolvedVolume` mora em
`app.infrastructure.filesystem`, que `test_application_architecture.py`
proíbe um caso de uso de importar.

**A alternativa, e por que foi recusada.** Mover `VolumeIdentityProvider`
e `ResolvedVolume` inteiros para o Domain arrastaria `ctypes` e `kernel32`
junto -- o adaptador do Windows divide esse módulo com a porta -- e separar
os dois é uma refatoração do RFC-027 dentro de um RFC sobre rotas HTTP.
Repetir quatro campos sai mais barato, e a repetição não fica duplicação
por muito tempo: o adaptador converte na própria fronteira, e nada acima
dele vê o tipo da Infrastructure (RFC-031 §4.2 do build prompt).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.value_objects.device_id import VolumeIdentity


@dataclass(frozen=True)
class MountedVolume:
    """Um volume conectado: o nome durável dele, e onde ele está agora.

    Divide-se em dois exatamente como `ResolvedVolume`, e a divisão é o
    ponto. `identity` é durável e é persistida. `mount_point` não é, e não
    pode ser: vale até o usuário desplugar o disco ou reiniciar, e por isso
    viaja num valor como este, e não numa coluna (RFC-027 §4).
    """

    identity: VolumeIdentity
    mount_point: Path
    """Onde o volume está conectado *neste instante* -- `D:\\`, tipicamente.

    **Nunca persista isto, e nunca o guarde em cache entre requisições.** É
    função da ordem de montagem, e guardá-lo é o defeito que o RFC-027
    existe para eliminar. Nada avisa este processo quando um cabo é puxado,
    então uma resposta lembrada fica errada a partir daquele momento e
    continua errada.
    """

    filesystem_label: str | None = None
    """O que o próprio volume informa -- muitas vezes vazio, muitas vezes `Untitled`.

    Informativo, nunca um identificador. `Device.label` é onde fica o nome
    que o usuário dá a um disco, justamente porque com este não dá para
    contar nem que exista nem que seja único.
    """

    total_bytes: int | None = None
    """A capacidade do volume, ou `None` quando a plataforma não informa.

    O campo que torna `VolumeCatalog.list_mounted()` mais caro que
    `mount_points()`: lê-lo custa um `GetDiskFreeSpaceExW` por volume, o que
    pode acordar um disco externo adormecido (RFC-031 §4.1 do build prompt).
    """
