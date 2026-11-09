"""O ambiente das bibliotecas de modelo, preparado antes do import delas (RFC-035 5.2).

O `huggingface_hub` lê `HF_HUB_OFFLINE` e `HF_HOME` uma vez, no primeiro
import, para constantes do módulo (`huggingface_hub/constants.py`); pôr
qualquer uma das duas no ambiente depois não muda nada (§2.3). O inductor do
torch lê `TORCHINDUCTOR_CACHE_DIR` a cada chamada, e cria
`%TEMP%\\torchinductor_<user>` quando a variável falta. Por isso o `__init__`
do pacote chama isto antes de qualquer outro import do pacote, e os únicos
que importam `torch` e `transformers` em `backend/app` estão dentro do
pacote (`tests/test_model_imports_architecture.py` mantém assim).

`os.environ` fora de `config/`, que o RFC-004 reserva para ler a
configuração: isto não é a configuração do app, é o ambiente de uma
biblioteca, e mora no pacote do adaptador que a usa -- o precedente é o
`discard_libpq_environment()` do RFC-034.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.infrastructure.config.layout import Layout

OFFLINE_VARIABLE = "HF_HUB_OFFLINE"


def huggingface_home(where: Layout) -> Path:
    """O que o `huggingface_hub` pode escrever: talvez nada, com o modo offline."""
    return where.cache_directory / "huggingface"


def inductor_cache(where: Layout) -> Path:
    """A pasta que o `torch` cria quando um modelo é carregado pelo `transformers`."""
    return where.cache_directory / "torchinductor"


def prepare_model_environment(where: Layout) -> None:
    """Desliga a rede e mantém as escritas das bibliotecas na raiz de dados.

    **Sobrescreve, sem olhar o que havia.** Instalado, o ambiente não é fonte
    de configuração (RFC-034 §5.2), e um `HF_HUB_OFFLINE=0` deixado por outro
    programa não pode religar a rede. Num checkout, sobrescreve pela mesma
    regra, para que um caminho que chega ao Hub quebre onde alguém lê o
    traceback, e não só na máquina de um usuário (RFC-035 §5.3).

    Três atribuições e mais nada: nenhum diretório é criado e o disco não é
    tocado, porque isto roda em todo import do pacote -- inclusive no do
    `FakeEmbeddingModel` da suíte rápida. As bibliotecas criam as pastas
    delas quando precisam.
    """
    os.environ[OFFLINE_VARIABLE] = "1"
    os.environ["HF_HOME"] = str(huggingface_home(where))
    os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(inductor_cache(where))
