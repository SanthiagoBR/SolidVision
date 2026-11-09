"""Todo disco que o sistema conhece, pronto para virar uma barra lateral (RFC-031 §4).

Duas linhas de trabalho e uma decisão. As linhas vêm de
`DeviceRepository.list()`; tudo o que só é verdade neste instante vem de
`DescribeDevicesUseCase`, onde a garantia de "uma enumeração, qualquer que
seja N" mora e é explicada. O que sobra aqui é a ordem.
"""

from __future__ import annotations

from app.application.use_cases.describe_devices import (
    DescribeDevicesUseCase,
    DeviceSummary,
)
from app.domain.repositories.device_repository import DeviceRepository


class ListDevicesUseCase:
    """Lista todo dispositivo conhecido, decorado com o que é verdade agora."""

    def __init__(
        self,
        device_repository: DeviceRepository,
        describer: DescribeDevicesUseCase,
    ) -> None:
        """Compõe a partir do repositório e do describer que as outras rotas usam.

        Composto em vez de autossuficiente, no modelo de
        `GetImageDetailsUseCase` recebendo um `ResolveImageLocationUseCase`:
        a decoração é o mesmo trabalho de que `POST /devices` e `PATCH
        /devices/{id}` precisam para um dispositivo, e uma cópia dele aqui
        seria a cópia que deixa de concordar.
        """
        self._devices = device_repository
        self._describer = describer

    def execute(self) -> list[DeviceSummary]:
        """Devolve todo dispositivo, com os fatos mais novos, ordenado para exibir.

        Ordenado pelo rótulo, porque `DeviceRepository.list()` deixa a ordem,
        de forma explícita, para quem desenha o resultado -- e uma barra
        lateral cujos discos trocassem de lugar entre duas leituras seria
        pior que uma em qualquer ordem fixa. Sem distinguir maiúsculas,
        porque o usuário digitou esses nomes; com o id como desempate, para
        que dois discos com o mesmo nome ainda voltem numa ordem estável, e
        não na ordem em que o banco os devolveu.

        A ordenação acontece **depois** da descrição, e não dentro dela,
        para que `DescribeDevicesUseCase` cumpra a promessa de preservar a
        ordem que recebeu -- a promessa que deixa o mesmo objeto servir a
        quem tem um dispositivo só.
        """
        summaries = self._describer.execute(self._devices.list())
        return sorted(
            summaries,
            key=lambda summary: (
                summary.device.label.casefold(),
                str(summary.device.id),
            ),
        )
