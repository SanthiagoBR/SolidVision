"""Localiza agora os arquivos de um dispositivo, pela enumeração de volumes (RFC-029).

O adaptador por trás de `DeviceLocator`: uma casca fina sobre
`VolumeIdentityProvider.mounted_volumes()`, que é a resposta do RFC-027 §7 a "este
disco está conectado" -- uma pergunta feita, nunca uma coluna lida.

**Nada aqui guarda cache, e a ausência é o desenho.** Um ponto de montagem em
cache está errado a partir do momento em que o usuário puxa o cabo, e o Windows
não avisa este processo quando isso acontece. Toda chamada enumera de novo. É
também isso que torna a coisa testável: um job criado enquanto um disco estava
plugado pode ser assumido depois que o disco se foi, e é esta a camada que
percebe.

Este é o único arquivo do RFC-029 que sabe em que plataforma está, e mesmo isso
indiretamente -- ele depende da porta, não do `kernel32`. Jobs, a assunção de
jobs, o reaper e o checkpoint não sabem nada de Windows, então um adaptador de
volume para Linux seria um arquivo novo, e não uma reescrita (RFC-029 §6).
"""

from __future__ import annotations

import os
from pathlib import Path

from app.domain.entities.device import Device
from app.domain.services.device_locator import DeviceLocator, FolderEntry
from app.domain.value_objects.job_scope import JobScope
from app.infrastructure.filesystem.volume_identity_provider import (
    VolumeIdentityProvider,
)
from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)


class MountedDeviceLocator(DeviceLocator):
    """Responde a partir do que o sistema operacional relata como montado, sempre."""

    def __init__(self, volume_provider: VolumeIdentityProvider) -> None:
        self._volumes = volume_provider

    def mount_point(self, device: Device) -> Path | None:
        """Devolve onde `device` está ligado neste instante, ou `None`.

        Um dispositivo está conectado exatamente quando a sua identidade de
        volume está entre as montadas -- todo o RFC-027 §7. `None` é a resposta
        honesta para um disco na gaveta, e quem chama a transforma em
        `DeviceNotConnectedError` quando precisava dos bytes.
        """
        return self._volumes.mounted_volumes().get(device.volume_identity)

    def resolve_scope(self, device: Device, scope: JobScope) -> JobScope | None:
        """Devolve o escopo como o sistema de arquivos o escreve, ou `None` se ausente.

        Três coisas acontecem aqui, e a terceira é a que é fácil deixar de fora.

        1. O escopo é unido ao ponto de montagem e resolvido. Resolver é o que
           transforma `Fotos` na grafia real do diretório, de que
           `normalize_scopes()` precisa: ele compara as partes exatamente, então
           duas grafias de uma mesma pasta sem distinção de maiúsculas
           sobreviveriam ambas à normalização e seriam percorridas duas vezes.
        2. Precisa ser um diretório. Um arquivo não é um escopo, nem um caminho
           que simplesmente não está lá.
        3. **Precisa continuar dentro do dispositivo.** `JobScope` já recusou
           `..`, uma âncora de unidade e um compartilhamento UNC -- mas uma
           junction NTFS dentro do escopo aponta para outro volume e parece uma
           pasta comum até ser resolvida. `..` não é a única saída de uma raiz,
           então a checagem é feita contra o caminho resolvido, e não contra o
           texto.

        Um dispositivo que não está montado responde `None` para todo escopo.
        Quem chama verifica a conexão antes, para que o usuário ouça qual disco
        plugar, e não que a pasta dele está errada.
        """
        located = self._locate(device, scope)
        if located is None:
            return None

        root, resolved = located
        if resolved == root:
            return JobScope()
        return JobScope(resolved.relative_to(root))

    def list_folders(self, device: Device, scope: JobScope) -> list[FolderEntry] | None:
        """Devolve as pastas logo dentro de `scope`, com `has_children` preenchido.

        Passa por `_locate()` em vez de unir o ponto de montagem ao escopo por
        conta própria, para que uma junction dentro do escopo seja pega aqui
        exatamente como é pega por `resolve_scope()` -- uma listagem que
        escapasse do dispositivo estaria lendo o `C:` do usuário por uma rota
        cujo propósito todo é que nenhum caminho enviado pelo cliente chegue a
        um sistema de arquivos (RFC-031 §5.1).

        **`has_children` é o que este método custa.** Saber se `2018/janeiro`
        tem subpastas próprias significa abrir `2018/janeiro`, então uma
        listagem de quarenta pastas são quarenta e uma chamadas a `readdir`, e
        não uma, e num disco mecânico frio essa é a leitura que o RFC-029 §7.2
        achou dominante. Duas coisas seguram a conta, e nenhuma é opcional:

        * a varredura dos filhos para cedo. `any()` sobre um gerador de
          `scandir` para na primeira entrada que é diretório, então uma pasta
          cuja primeira entrada é uma subpasta custa uma entrada, e não uma
          listagem inteira;
        * nada desce além disso. A resposta é um booleano, não uma contagem, e
          o RFC-031 §2.3 recusa o percurso da subárvore que uma contagem
          exigiria.

        Um filho que não abre -- uma barreira de permissão, um disco puxado no
        meio da listagem -- é relatado como `has_children=False` e registrado no
        log. Ele não pode encerrar a listagem: uma pasta protegida não é razão
        para deixar de mostrar as pastas que responderam, que é a regra que
        `mounted_volumes()` já aplica aos volumes que se recusam a ser nomeados.

        Arquivos são pulados. O que isto alimenta é um seletor de pastas cuja
        saída volta como `scopes[]`, e um arquivo não é um escopo.
        """
        located = self._locate(device, scope)
        if located is None:
            return None

        _, resolved = located
        entries: list[FolderEntry] = []
        try:
            with os.scandir(resolved) as scan:
                for entry in scan:
                    if not self._is_directory(entry):
                        continue
                    entries.append(
                        FolderEntry(
                            name=entry.name,
                            has_children=self._has_subdirectory(Path(entry.path)),
                        )
                    )
        except OSError:
            # A pasta foi resolvida há um instante e agora não abre -- o disco
            # sumiu entre as duas chamadas. "Não é uma pasta deste dispositivo"
            # é a resposta honesta, e a que quem chama já sabe relatar.
            return None
        return entries

    def _locate(self, device: Device, scope: JobScope) -> tuple[Path, Path] | None:
        """Devolve `(raiz do dispositivo, escopo resolvido)`, ou `None` se não é pasta.

        Compartilhado por `resolve_scope()` e `list_folders()` em vez de escrito
        duas vezes. A checagem de contenção abaixo é a propriedade de segurança
        de ambos, e duas cópias dela seriam duas chances de uma delas a perder
        em silêncio.

        Três coisas acontecem aqui, e a terceira é a que é fácil deixar de fora.

        1. O escopo é unido ao ponto de montagem e resolvido. Resolver é o que
           transforma `Fotos` na grafia real do diretório, de que
           `normalize_scopes()` precisa: ele compara as partes exatamente, então
           duas grafias de uma mesma pasta sem distinção de maiúsculas
           sobreviveriam ambas à normalização e seriam percorridas duas vezes.
        2. Precisa ser um diretório. Um arquivo não é um escopo, nem um caminho
           que simplesmente não está lá.
        3. **Precisa continuar dentro do dispositivo.** `JobScope` já recusou
           `..`, uma âncora de unidade e um compartilhamento UNC -- mas uma
           junction NTFS dentro do escopo aponta para outro volume e parece uma
           pasta comum até ser resolvida. `..` não é a única saída de uma raiz,
           então a checagem é feita contra o caminho resolvido, e não contra o
           texto.
        """
        mount = self.mount_point(device)
        if mount is None:
            return None

        try:
            root = mount.resolve()
            target = (root / str(scope)) if scope.parts else root
            resolved = target.resolve()
            if not resolved.is_dir():
                return None
            if resolved != root and not resolved.is_relative_to(root):
                return None
        except OSError:
            # Um caminho sobre o qual a plataforma não responde -- uma montagem
            # de rede desconectada, uma barreira de permissão -- é "não é uma
            # pasta deste dispositivo", e não uma queda; é isso que quem chama
            # pergunta, e o que vai relatar como escopo inválido.
            return None

        return root, resolved

    @staticmethod
    def _is_directory(entry: os.DirEntry[str]) -> bool:
        """Se uma entrada listada é pasta, tolerando uma que não diz o que é."""
        try:
            return entry.is_dir()
        except OSError:
            logger.warning(
                "Skipping %s: the platform would not classify it", entry.path
            )
            return False

    @staticmethod
    def _has_subdirectory(folder: Path) -> bool:
        """Se `folder` contém pelo menos uma pasta, parando na primeira.

        O gerador dentro de `any()` é o que torna isto barato: `scandir` produz
        preguiçosamente, então uma pasta cuja primeira entrada é um diretório
        custa a leitura de uma entrada, e não a listagem inteira de uma pasta
        que pode ter milhares de fotos.
        """
        try:
            with os.scandir(folder) as scan:
                return any(entry.is_dir() for entry in scan)
        except OSError:
            logger.warning("Cannot look inside %s; reporting it as a leaf", folder)
            return False
