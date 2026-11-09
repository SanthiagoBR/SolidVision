"""Abre o Explorer com um arquivo selecionado, e nada mais (RFC-030 §5).

O adaptador por trás de `FileRevealerPort`, que mora no domínio ao lado das
outras portas (`app/domain/services/file_revealer_port.py`) porque o caso de uso
que a chama não pode importar a infraestrutura. O RFC-030 §11 listava "porta +
adaptador Windows" neste arquivo; a porta se mudou e o adaptador não.

**Só Windows, de propósito** (RFC-030 §5.3). `open -R` no macOS e a chamada
D-Bus `org.freedesktop.FileManager1` no Linux estão declarados e não escritos:
um adaptador que nunca rodou é uma promessa que ninguém verificou.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from app.domain.services.file_revealer_port import FileRevealerPort
from app.domain.value_objects.image_path import ImagePath

Launcher = Callable[..., object]
"""O que inicia o processo: `subprocess.Popen`, ou um gravador nos testes."""


class WindowsFileRevealer(FileRevealerPort):
    """`explorer.exe /select,<path>`, sem shell e sem esperar."""

    def __init__(self, launch: Launcher = subprocess.Popen) -> None:
        """Recusa existir fora do Windows, como `WindowsVolumeIdentityProvider`.

        `launch` é a costura que os testes usam para que nada na suíte abra uma
        janela real do Explorer; a produção nunca o passa.
        """
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsFileRevealer requires Windows; this process is running "
                f"on {sys.platform!r}."
            )
        self._launch = launch

    def reveal(self, path: ImagePath) -> None:
        """Abre o Explorer com `path` selecionado, ou levanta exceção se nada há lá.

        Quatro decisões, e a versão óbvia erra cada uma delas:

        * **Uma lista de argumentos, e `shell=False`.** Nenhum shell vê o
          caminho, então nenhum caractere dele pode significar outra coisa
          além de si mesmo. O caminho veio da própria linha do servidor unida a
          um ponto de montagem, não do cliente (RFC-030 §5.1), e ainda assim
          esta é a segunda barreira, e não a única.
        * **`/select,` e o caminho são dois argumentos, não um.** O RFC-030
          §5.1 escreveu `f"/select,{path}"` como um único item da lista, e isso
          só funciona até o caminho ter um espaço: o `subprocess` então põe o
          item inteiro entre aspas, o Explorer recebe
          `"/select,F:\fotos\fazenda São João.jpg"`, não reconhece a chave
          dentro das aspas e abre a pasta Documentos do usuário sem nada
          selecionado. Como dois itens, a linha de comando é
          `/select, "F:\fotos\fazenda São João.jpg"`, que seleciona o arquivo.
          Verificado no Windows 10 abrindo as duas formas e lendo de volta a
          seleção do Explorer por `Shell.Application`
          (`experiments/rfc-030-file-access/verify_explorer_select.log`).
        * **`explorer.exe` pelo caminho absoluto, sob `%SystemRoot%`.** Com
          `shell=False`, o Windows resolve um `explorer` sem caminho procurando
          no diretório do executável do Python e no diretório atual *antes*
          dos do sistema -- então um `explorer.exe` perdido na pasta `Scripts`
          de um virtualenv seria o que rodaria.
        * **`Popen`, nunca `run`.** O RFC-030 §5.1 escreveu
          `subprocess.run(..., check=False)`, e a metade `check=False` estava
          certa: o Explorer sai com código diferente de zero quando dá certo,
          então o seu status não significa nada. A metade `run` espera o
          processo terminar, e com "abrir janelas de pastas em processos
          separados" ligado esse processo é a janela -- a requisição ficaria
          pendurada até o usuário fechá-la. 204 significa *iniciado*, que é
          tudo o que alguém poderia saber.

        O caminho é checado antes de iniciar porque o Explorer não falha para
        um arquivo ausente: ele abre em silêncio uma pasta padrão, e o usuário
        veria uma janela sem relação nenhuma, sem explicação.
        """
        target = path.value
        if not target.is_file():
            raise FileNotFoundError(f"No file at {path}")

        self._launch(
            [str(_explorer_executable()), "/select,", os.fspath(target)],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _explorer_executable() -> Path:
    """O `explorer.exe` do sistema, nunca o que a ordem de busca achar."""
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
