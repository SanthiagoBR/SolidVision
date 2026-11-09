"""Identidade de volume: a porta, e o adaptador Windows por trás dela (RFC-027).

Este módulo responde a duas perguntas e se recusa a responder a uma terceira.

*Em que volume está este caminho?* -- `resolve()`, que devolve um identificador
que o sistema operacional garante ser estável entre remontagens, reinicializações
e portas, junto com tudo o mais que valha saber enquanto o disco está por acaso
diante de nós.

*O que está montado agora?* -- `mounted_volumes()`, que é como o RFC-027 §7
transforma "o HD3 está conectado" numa pergunta com resposta fresca, em vez de
numa coluna velha.

*Onde mora o dispositivo X?* não tem resposta própria aqui, e isso é
deliberado: é `mounted_volumes()` consultado pela identidade, então o código
que quer uma letra de unidade tem de passar pela enumeração que acabou de ser
feita, e não por algo lembrado.

**Só o adaptador Windows é entregue.** `VolumeKind` declara os discriminadores
de Linux e macOS para que acrescentar esses adaptadores não exija migration, mas
um adaptador que ninguém rodou seria uma promessa de portabilidade que ninguém
verificou (RFC-027 §4.1).
"""

from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.value_objects.device_id import VolumeIdentity, VolumeKind


class VolumeIdentityError(RuntimeError):
    """Levantado quando o sistema operacional não identifica um volume.

    Uma falha de infraestrutura e não um `DomainError`: o domínio não tem
    opinião sobre o `kernel32`. É levantado, nunca engolido num `None`, porque
    todo chamador está prestes a indexar dados pela resposta -- adivinhar uma
    identidade arquivaria as fotos de um disco sob o dispositivo errado, o que
    é pior do que falhar.
    """


@dataclass(frozen=True)
class ResolvedVolume:
    """Tudo o que se sabe de um volume enquanto ele está de fato montado.

    Divide-se limpamente em dois, e a divisão é o que importa. `identity` é
    durável e é persistida. `mount_point` não é, e não é persistido: vale até o
    usuário desplugar o disco ou reiniciar, e é por isso que viaja num valor de
    retorno e não numa coluna (RFC-027 §4).
    """

    identity: VolumeIdentity
    mount_point: Path
    """Onde o volume está ligado *neste instante* -- `D:\\`, tipicamente.

    Nunca persista isto. É função da ordem de montagem, e guardá-lo é o defeito
    que o RFC-027 existe para remover.
    """

    filesystem_label: str | None = None
    total_bytes: int | None = None


class VolumeIdentityProvider(ABC):
    """Porta para perguntar ao sistema operacional em que volume está um caminho."""

    @abstractmethod
    def resolve(self, path: Path) -> ResolvedVolume:
        """Identifica o volume que contém `path`, que deve existir e estar montado.

        Levanta `VolumeIdentityError` quando a plataforma não consegue responder
        -- um disco desconectado, um compartilhamento de rede sem identidade de
        volume, um caminho que não existe. Não há recurso à letra de unidade:
        uma letra no lugar de uma identidade seria indistinguível de uma
        verdadeira e recriaria o bug em silêncio (RFC-027 §2.1).
        """

    @abstractmethod
    def mounted_volumes(self) -> dict[VolumeIdentity, Path]:
        """Devolve todo volume montado agora, mapeado para o seu ponto de montagem.

        Toda a checagem de conexão do RFC-027 §7: um dispositivo está conectado
        exatamente quando a sua `volume_identity` é chave deste mapeamento, e o
        valor é onde encontrá-lo. Quem chama pode guardar o resultado em cache
        durante uma requisição e não pode guardá-lo entre requisições -- a
        resposta muda quando um usuário puxa um cabo, e nada avisa este processo
        quando isso acontece.
        """


class WindowsVolumeIdentityProvider(VolumeIdentityProvider):
    r"""Adaptador Windows sobre o `kernel32`, via `ctypes` e sem nova dependência.

    Usa `\\?\Volume{GUID}\` -- o nome que o próprio Windows usa internamente
    para o volume, independente de onde ele está montado. Os dois candidatos
    rejeitados, e por quê (RFC-027 §4.1): a letra de unidade não é nada
    estável, e o número de série de 32 bits do volume, de
    `GetVolumeInformation`, é estável mas só tem 32 bits, o que não basta para
    servir de chave de uma tabela.

    O GUID muda quando o volume é reformatado. Isso é aceito e não precisa de
    tratamento: reformatar destrói as fotos, então deixar as linhas delas
    órfãs é o desfecho correto.

    A única limitação que fica de pé é que um clone byte a byte de um disco
    carrega o mesmo GUID, e os dois são indistinguíveis aqui (RFC-027 §13).
    Declarada, não resolvida.
    """

    _BUFFER_LENGTH = 512
    """Com folga além de `MAX_PATH` (260).

    Um nome de volume por GUID tem 49 caracteres, mas o caminho de uma pasta
    montada pode passar de `MAX_PATH` num Windows moderno, então o buffer é
    dimensionado para tornar o truncamento impossível, e não só improvável.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise VolumeIdentityError(
                "WindowsVolumeIdentityProvider requires Windows; this process "
                f"is running on {sys.platform!r}."
            )

    def resolve(self, path: Path) -> ResolvedVolume:
        """Sobe de `path` até o seu ponto de montagem, e então nomeia o volume."""
        mount_point = self._volume_path_name(path)
        identity = VolumeIdentity(
            value=self._volume_guid_name(mount_point),
            kind=VolumeKind.WINDOWS_VOLUME_GUID,
        )
        return ResolvedVolume(
            identity=identity,
            mount_point=mount_point,
            filesystem_label=self._filesystem_label(mount_point),
            total_bytes=self._total_bytes(mount_point),
        )

    def mounted_volumes(self) -> dict[VolumeIdentity, Path]:
        """Nomeia cada ponto de montagem com letra que o Windows relata agora.

        Construído a partir de `GetLogicalDriveStrings` e não de
        `FindFirstVolume`, porque um volume sem caminho de montagem não é algo
        com que esta aplicação possa fazer nada: todo chamador ou junta um
        `relative_path` a ele ou o mostra a um usuário.

        Volumes que se recusam a ser nomeados são pulados, e não viram exceção.
        Uma unidade óptica vazia e um volume BitLocker bloqueado falham ambos
        aqui, e nenhum dos dois é razão para deixar de relatar os discos que
        responderam.
        """
        mounted: dict[VolumeIdentity, Path] = {}
        for mount_point in self._logical_drive_mount_points():
            try:
                guid_name = self._volume_guid_name(mount_point)
            except VolumeIdentityError:
                continue
            identity = VolumeIdentity(
                value=guid_name, kind=VolumeKind.WINDOWS_VOLUME_GUID
            )
            mounted[identity] = mount_point
        return mounted

    @staticmethod
    def _kernel32() -> Any:
        """Devolve o `kernel32`, carregado com os pontos de entrada wide-character.

        Obtido a cada chamada, e não guardado num global do módulo, para que
        importar este módulo continue gratuito em qualquer plataforma: a maior
        parte da suíte de testes chega a este pacote transitivamente, e
        `ctypes.WinDLL` não existe fora do Windows.

        Tipado como `Any` porque um handle de DLL do `ctypes` não tem assinatura
        estática contra a qual checar -- cada atributo dele é resolvido em tempo
        de execução, pelo nome, e anotá-lo de forma mais estrita descreveria uma
        garantia que o `ctypes` não dá.
        """
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def _volume_path_name(self, path: Path) -> Path:
        r"""Devolve o ponto de montagem sob o qual `path` está, p. ex. `D:\`.

        `GetVolumePathName` é o que faz uma pasta montada funcionar tão bem
        quanto uma letra de unidade: para `D:\photos\2018` ele responde `D:\`,
        e para um volume montado em `C:\mnt\archive` responde `C:\mnt\archive\`
        e não `C:\`. Usar `path.anchor` no lugar estaria certo para letras de
        unidade e silenciosamente errado para toda pasta montada.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        if not self._kernel32().GetVolumePathNameW(
            str(path), buffer, self._BUFFER_LENGTH
        ):
            raise VolumeIdentityError(
                f"Could not determine the mount point of {path} "
                f"(Windows error {ctypes.get_last_error()})."
            )
        return Path(buffer.value)

    def _volume_guid_name(self, mount_point: Path) -> str:
        r"""Devolve `\\?\Volume{GUID}\` para um ponto de montagem.

        O ponto de montagem precisa chegar ao Windows com um separador no fim;
        `GetVolumeNameForVolumeMountPoint` falha de cara sem ele, e
        `str(Path("D:/mnt/archive"))` não tem nenhum, então ele é acrescentado
        aqui em vez de presumido.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        if not self._kernel32().GetVolumeNameForVolumeMountPointW(
            self._with_trailing_separator(mount_point), buffer, self._BUFFER_LENGTH
        ):
            raise VolumeIdentityError(
                f"Could not read the volume identity of {mount_point} "
                f"(Windows error {ctypes.get_last_error()})."
            )
        return buffer.value

    def _filesystem_label(self, mount_point: Path) -> str | None:
        """Devolve o rótulo do próprio volume, ou `None` quando ele não tem um.

        Só informativo. Vazio é normal -- um disco sem rótulo relata `""` -- e
        volta como `None` e não como string vazia, para que "sem rótulo" seja um
        valor e não dois.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        if not self._kernel32().GetVolumeInformationW(
            self._with_trailing_separator(mount_point),
            buffer,
            self._BUFFER_LENGTH,
            None,
            None,
            None,
            None,
            0,
        ):
            return None
        return buffer.value or None

    def _total_bytes(self, mount_point: Path) -> int | None:
        """Devolve a capacidade do volume, ou `None` quando o Windows não a informa."""
        total = ctypes.c_ulonglong(0)
        if not self._kernel32().GetDiskFreeSpaceExW(
            self._with_trailing_separator(mount_point),
            None,
            ctypes.byref(total),
            None,
        ):
            return None
        return int(total.value)

    def _logical_drive_mount_points(self) -> list[Path]:
        """Devolve, como caminhos, cada raiz de letra de unidade que o Windows tem.

        `GetLogicalDriveStrings` enche o buffer com uma sequência de strings
        terminadas em NUL, fechada por um segundo NUL, então a divisão abaixo é
        a forma documentada de ler o resultado, e não manipulação de string
        fazendo as vezes de um parser.
        """
        buffer = ctypes.create_unicode_buffer(self._BUFFER_LENGTH)
        length = self._kernel32().GetLogicalDriveStringsW(self._BUFFER_LENGTH, buffer)
        if not length:
            raise VolumeIdentityError(
                "Could not enumerate mounted drives "
                f"(Windows error {ctypes.get_last_error()})."
            )
        # Fatiado e reunido em vez de lido por `buffer.value`, que para no
        # primeiro NUL e por isso relataria só a primeira unidade. `"".join()`
        # está certo quer a fatia volte como string, quer como sequência de
        # caracteres.
        raw = "".join(buffer[:length])
        return [Path(entry) for entry in raw.split("\0") if entry]

    @staticmethod
    def _with_trailing_separator(mount_point: Path) -> str:
        text = str(mount_point)
        return text if text.endswith(("\\", "/")) else text + "\\"
