"""Onde as coisas estão: o modo, a raiz e o diretório de dados do usuário (RFC-034 4).

O único módulo de `backend/app` que usa `__file__`, o diretório de trabalho
ou `LOCALAPPDATA`; `tests/test_path_anchors_architecture.py` mantém assim.
Não importa nem `settings` nem o logger, e os dois o importam: o layout é a
geometria dos diretórios, não configuração.

A árvore é a mesma nos dois modos (§4.1)::

    <root>\\
        backend\\           app\\, alembic\\, alembic.ini
        runtime\\python\\    RFC-041: o interpretador embarcado, que acha
                           `backend` pelo seu `python312._pth`
        runtime\\postgres\\  RFC-033
        runtime\\models\\    RFC-035
        frontend\\dist\\     RFC-037: a interface que a API serve
        pyproject.toml     só num checkout
        .env               só num checkout

Instalado, a raiz é a pasta `resources\\` do app Electron (RFC-041 §4), e o
`parents[4]` deste arquivo cai nela como cai na raiz do repositório num
checkout.

Um `Layout` pode ser montado à mão, com qualquer raiz e qualquer diretório de
dados. É assim que os testes exercitam o modo instalado de dentro de um
checkout, sem estado global.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

CHECKOUT_MARKER = "pyproject.toml"
"""O que faz de um diretório um checkout: `pytest`, `mypy` e `ruff` dependem
dele, e um instalador não tem motivo para levá-lo. `.git` não serviria -- um
checkout exportado não tem (§4.2)."""

INSTALLED_DATA_NAME = "SolidVision"
DEVELOPMENT_DATA_NAME = "SolidVision-dev"
"""Irmã da pasta instalada, não uma pasta dentro dela: o que um desinstalador
futuro fizer com os dados do usuário não pode atingir os de um desenvolvedor
(§6.2)."""

CHECKOUT_CONFIG_NAME = ".env"
USER_CONFIG_NAME = "solidvision.env"


class Environment(StrEnum):
    """O modo. Um fato lido do disco, nunca um valor que alguém declara."""

    DEVELOPMENT = "development"
    INSTALLED = "installed"


@dataclass(frozen=True)
class Layout:
    """O modo, e os dois diretórios de que todo o resto deriva."""

    environment: Environment
    root: Path
    """A raiz do repositório, ou a da instalação."""
    data_directory: Path
    """`%LOCALAPPDATA%\\SolidVision-dev`, ou `%LOCALAPPDATA%\\SolidVision`."""

    def __post_init__(self) -> None:
        for name in ("root", "data_directory"):
            path: Path = getattr(self, name)
            if not path.is_absolute():
                raise ValueError(f"A layout's {name} must be absolute, not {path}.")

    # Tudo abaixo é derivado, e nada disso toca o disco.

    @property
    def config_file(self) -> Path:
        """O único arquivo que este modo lê, e nunca o do outro modo (§5.1)."""
        if self.environment is Environment.DEVELOPMENT:
            return self.root / CHECKOUT_CONFIG_NAME
        return self.data_directory / USER_CONFIG_NAME

    @property
    def config_base(self) -> Path:
        """A pasta de `config_file`: em relação a que um setting de caminho
        relativo é relativo (§6.3, regra 4)."""
        return self.config_file.parent

    @property
    def backend_directory(self) -> Path:
        """Onde ficam `alembic.ini` e `alembic\\`."""
        return self.root / "backend"

    @property
    def postgres_runtime(self) -> Path:
        """Os binários do PostgreSQL embarcados no repositório (RFC-033 §5.2)."""
        return self.root / "runtime" / "postgres"

    @property
    def model_runtime(self) -> Path:
        """Os dois modelos, materializados a partir do lockfile (RFC-035 §4.2).

        Por máquina e somente leitura quando instalado, ao lado de
        `runtime\\postgres`.
        """
        return self.root / "runtime" / "models"

    @property
    def frontend_directory(self) -> Path:
        """O build da interface, que a API serve (RFC-037 §5.1).

        `frontend\\dist`, onde `npm run build` escreve num checkout. Instalado,
        `resources\\frontend\\dist`: ao lado do código e fora do arquivo do
        Electron, porque quem lê é o Python (RFC-041 §4).
        """
        return self.root / "frontend" / "dist"

    @property
    def log_directory(self) -> Path:
        return self.data_directory / "logs"

    @property
    def thumbnail_directory(self) -> Path:
        return self.data_directory / "thumbnails"

    @property
    def postgres_directory(self) -> Path:
        """O cluster do usuário; o adaptador acrescenta a versão major abaixo dele."""
        return self.data_directory / "postgres"

    @property
    def cache_directory(self) -> Path:
        """O que o runtime dos modelos escreve (RFC-035 §5.4).

        Nada ali é fonte de coisa nenhuma, então tudo pode ser apagado sem
        perda, pelo usuário ou por um desinstalador.
        """
        return self.data_directory / "cache"

    @property
    def query_image_directory(self) -> Path:
        """Onde uma foto enviada como consulta de busca espera o modelo (RFC-038).

        `cache\\query-images`: cada arquivo vive por uma requisição e é apagado
        seja qual for o fim dela, e a API esvazia a pasta ao iniciar, pelo que
        um processo que morreu deixou para trás. Não `%TEMP%`, pelo mesmo
        motivo de as escritas do runtime dos modelos não estarem lá: o app
        guarda o que escreve na própria raiz de dados (RFC-035 §5.4).
        """
        return self.cache_directory / "query-images"


def layout_at(root: Path, profile: Path) -> Layout:
    """O layout do código sob `root`, para um usuário cujo perfil é `profile`.

    `development` precisa de prova e `installed` é o que sobra, porque os dois
    erros não custam o mesmo (§4.2). A máquina de um usuário tomada por um
    checkout leria o ambiente e escreveria em `SolidVision-dev`, e no dia em
    que a detecção fosse corrigida os dados do usuário pareceriam ter sumido:
    silencioso e caro. Um checkout tomado por uma instalação ignora o `.env`
    e procura um cluster embarcado, e o desenvolvedor percebe na primeira
    execução.
    """
    if (root / CHECKOUT_MARKER).is_file():
        return Layout(Environment.DEVELOPMENT, root, profile / DEVELOPMENT_DATA_NAME)
    return Layout(Environment.INSTALLED, root, profile / INSTALLED_DATA_NAME)


def local_app_data(environ: Mapping[str, str] | None = None) -> Path:
    """A pasta de dados de aplicativo, por usuário e por máquina (§6.2).

    A variável primeiro: é como uma execução de validação aponta um processo
    para um diretório temporário sem tocar no perfil real. Sem ela -- um
    launcher pode montar do zero o ambiente dos filhos -- pergunta-se ao
    Windows, em vez de cada default adivinhar um fallback diferente.
    """
    source = os.environ if environ is None else environ
    variable = source.get("LOCALAPPDATA", "")
    if variable and Path(variable).is_absolute():
        return Path(variable)
    if sys.platform != "win32":
        # Uma raiz, para um processo que de todo modo não vai longe aqui: o
        # adaptador de volumes e o cluster embarcado são só para Windows.
        return Path.home() / ".local" / "share"
    return _known_local_app_data()


def _known_local_app_data() -> Path:
    """`FOLDERID_LocalAppData`, perguntado ao shell."""
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder = Guid(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )
    shell32: Any = ctypes.WinDLL("shell32")
    ole32: Any = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(Guid),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    # Um tipo de retorno `HRESULT` faz o ctypes levantar `OSError` numa falha.
    shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.c_void_p()
    shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(pointer))
    try:
        if pointer.value is None:
            raise OSError("Windows named no folder for FOLDERID_LocalAppData.")
        return Path(ctypes.wstring_at(pointer.value))
    finally:
        ole32.CoTaskMemFree(pointer)


def detect_layout() -> Layout:
    """O layout do código que está rodando.

    O único lugar que calcula a raiz, e portanto a única função a mudar num
    app congelado, em que a raiz vem do caminho do executável, e não de
    `__file__` (§4.1). Com um Python embarcado e o código numa árvore,
    `__file__` continua funcionando e nada muda.
    """
    return layout_at(Path(__file__).resolve().parents[4], local_app_data())


layout = detect_layout()
