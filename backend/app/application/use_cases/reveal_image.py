"""Mostrar uma foto indexada no gerenciador de arquivos (RFC-030 §5).

**A única entrada é um id.** O caminho que isto abre é montado aqui, a partir
da linha que o id nomeia e do ponto de montagem que o dispositivo tem agora;
nenhuma rota aceita um caminho num corpo, numa query ou num cabeçalho. É isso
que torna directory traversal, `..` e um caminho UNC para outra máquina
impossíveis, e não filtrados: não há string vinda do cliente para sanear
(RFC-030 §5.1).

As duas guardas do RFC-030 §6 -- a configuração do operador e a checagem de
loopback -- não estão aqui. As duas são fatos sobre o pedido HTTP e sobre a
implantação, e a rota as impõe antes de este caso de uso sequer ser
construído.
"""

from __future__ import annotations

from app.application.use_cases.resolve_image_location import (
    ResolveImageLocationUseCase,
)
from app.domain.exceptions import (
    DeviceNotConnectedError,
    FileGoneError,
    ImageNotFoundError,
)
from app.domain.repositories.image_repository import ImageRepository
from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.value_objects.image_id import ImageId


class RevealImageUseCase:
    """Abre o gerenciador de arquivos da plataforma com uma imagem selecionada."""

    def __init__(
        self,
        repository: ImageRepository,
        location_resolver: ResolveImageLocationUseCase,
        file_revealer: FileRevealerPort,
    ) -> None:
        self._repository = repository
        self._locations = location_resolver
        self._revealer = file_revealer

    def execute(self, image_id: ImageId) -> None:
        """Revela a imagem, ou diz com precisão por que ela não pode ser revelada.

        Três recusas, cada uma uma coisa diferente para o usuário fazer:

        * a imagem não existe -- `ImageNotFoundError`, 404;
        * o disco dela não está plugado -- `DeviceNotConnectedError`, 409,
          levantado por `Image.require_absolute_path()`, e não por uma segunda
          checagem escrita aqui. A mensagem nomeia o disco a buscar;
        * o disco está plugado e o arquivo não está nele -- `FileGoneError`,
          410. A foto foi apagada, movida ou renomeada depois da indexação, e
          buscar um disco não ajudaria.

        A checagem do arquivo ausente acontece no adaptador, que levanta
        `FileNotFoundError` como qualquer E/S levantaria, e é traduzida para o
        erro de domínio aqui: ler o disco é trabalho da Infrastructure, e
        decidir que um arquivo ausente é um 410, e não um 404, não é.
        """
        image = self._repository.get(image_id)
        if image is None:
            raise ImageNotFoundError(f"No indexed image with id {image_id.value}.")

        located = self._locations.execute_one(image)
        try:
            path = located.image.require_absolute_path()
        except DeviceNotConnectedError as error:
            # O mesmo erro, levantado de novo só para nomear o disco pelo
            # rótulo que o usuário deu: a entidade conhece o id do
            # dispositivo, e "plugue o 564e9201-..." não é um conselho que
            # alguém consiga seguir.
            raise DeviceNotConnectedError(
                f"{image.relative_path} is on {located.device.label!r}, which is "
                "not connected. Plug it in and try again."
            ) from error

        try:
            self._revealer.reveal(path)
        except FileNotFoundError as error:
            raise FileGoneError(
                f"{image.relative_path} is no longer on {located.device.label!r}. "
                "It was moved, renamed or deleted after it was indexed."
            ) from error
