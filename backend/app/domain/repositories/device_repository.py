"""Porta abstrata de repositório para a persistência dos dispositivos (RFC-027)."""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod

from app.domain.entities.device import Device
from app.domain.value_objects.device_id import DeviceId, VolumeIdentity


class DeviceRepository(ABC):
    """Contrato de repositório para gerenciar as entidades de domínio `Device`.

    Fica ao lado de `ImageRepository`, e não dentro dele: um dispositivo
    sobrevive a todas as imagens que tem, é criado por um ato diferente
    (plugar um disco) e é procurado por uma chave -- a identidade do volume --
    que nenhuma imagem carrega.

    Nada aqui informa se um dispositivo está conectado. Essa pergunta não tem
    resposta persistente (RFC-027 §7); ela é feita ao
    `VolumeIdentityProvider`, que enumera o que está montado *agora*, e uma
    implementação desta porta que ganhasse uma coluna `is_connected` a
    responderia com um valor que deixou de ser verdade no momento em que o
    usuário desplugou o disco.
    """

    @abstractmethod
    def save(self, device: Device) -> None:
        """Cria o dispositivo, ou atualiza a linha que já tem o id dele.

        Um upsert, e não uma criação única, porque o lugar natural da chamada
        é um worker começando uma execução contra um disco que ele pode ou não
        ter visto antes, e todo campo, menos a identidade, deve mudar entre
        execuções: `last_seen_at` sempre, `label` quando o usuário o renomeia,
        `total_bytes` quando o volume cresce, os contadores do scan depois de
        cada scan.

        O `id` é derivado de `volume_identity`, então salvar o mesmo volume
        duas vezes atualiza uma linha, em vez de criar uma segunda. Uma
        implementação tem, portanto, de usar o `id` como chave e deixar a
        restrição `UNIQUE` sobre a identidade como rede de segurança, e não
        como o mecanismo.
        """

    @abstractmethod
    def record_scan(
        self, device_id: DeviceId, file_count: int, scanned_at: datetime.datetime
    ) -> None:
        """Escreve o resultado de um scan do disco inteiro, e nada mais.

        `last_scan_file_count` e `last_scan_at` -- o denominador do número de
        "% indexado" e a data dele (RFC-027 §8) --, escritos pelo job do disco
        inteiro quando a listagem dele termina (RFC-039 §4.1).

        **Duas colunas, e não um `save()`.** `save()` reescreve a linha
        inteira a partir de uma cópia, e a cópia que o executor tem é do
        começo do job: uma renomeação feita depois -- o rótulo é editável com
        o disco em uso (RFC-031 §7) -- seria desfeita por um scan que
        terminasse depois dela. Isto escreve os dois campos que o scan detém,
        sobre o que quer que a linha diga agora.

        Um id desconhecido não escreve nada e não é erro: um dispositivo pode
        ser apagado enquanto o job dele roda, e o scan de um disco que ninguém
        mais conhece não tem para onde ir.
        """

    @abstractmethod
    def get(self, device_id: DeviceId) -> Device | None:
        """Busca um dispositivo pelo identificador, ou `None` se desconhecido."""

    @abstractmethod
    def get_by_volume_identity(self, identity: VolumeIdentity) -> Device | None:
        """Busca o dispositivo de um volume que o sistema operacional acabou de relatar.

        A consulta que torna remontar de graça. Um worker resolve a identidade
        do que quer que lhe tenham apontado, pergunta isto, e ou acha o disco
        que indexou no mês passado -- com outra letra de unidade, e isso não
        importa -- ou descobre que é um disco novo.

        A comparação é exata nas duas metades da identidade, string e tipo.
        Sem ignorar maiúsculas, sem normalizar separadores: o valor é opaco
        acima do adaptador que o cunhou, e uma implementação que, "para
        ajudar", ignorasse maiúsculas fundiria dois volumes numa plataforma
        cujos identificadores distinguem maiúsculas.
        """

    @abstractmethod
    def list(self) -> list[Device]:
        """Devolve todo dispositivo que o repositório conhece.

        A ordem não faz parte do contrato. Os chamadores que desenham uma
        lista de dispositivos -- uma barra lateral, por exemplo -- ordenam pelo
        que exibem.
        """

    @abstractmethod
    def delete(self, device_id: DeviceId) -> None:
        """Remove um dispositivo do repositório.

        Não faz nada quando o dispositivo não existe. As implementações não
        podem propagar a exclusão para `images`: uma linha de imagem é um
        embedding que custou tempo real de inferência, e o RFC-027 §6.3 é
        explícito: as linhas de um disco ausente são o registro de algo a
        plugar, nunca algo a apagar em silêncio. Um repositório apoiado num
        banco com chave estrangeira vai, portanto, recusar a exclusão enquanto
        houver imagens, e essa recusa é o comportamento correto.
        """
