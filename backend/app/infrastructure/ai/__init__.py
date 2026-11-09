"""Pacote de infraestrutura de IA.

**A primeira coisa que este pacote faz é preparar o ambiente das bibliotecas
de modelo** (RFC-035 §5.2): o modo offline, e onde `huggingface_hub` e
`torch` podem escrever. O Python roda o `__init__` de um pacote antes de
qualquer submódulo dele, e `torch` e `transformers` não são importados em
nenhum lugar de `backend/app` a não ser aqui, então nenhum caminho do app
chega a `huggingface_hub` sem passar antes por estas linhas. Elas vêm antes
de todos os outros imports abaixo, de propósito.

`ClipEmbeddingModel`, deliberadamente, NÃO é reexportado aqui. O
`__init__` deste pacote roda em qualquer import `from app.infrastructure.ai...`,
inclusive nos imports de `FakeEmbeddingModel` espalhados pela suíte rápida
de testes, e reexportar o adaptador CLIP arrastaria `torch` e `transformers`
para cada um desses imports sem ganho nenhum. Importe-o pelo caminho
completo do módulo: `from app.infrastructure.ai.clip_embedding_model import
ClipEmbeddingModel`.
"""

from app.infrastructure.ai.environment import prepare_model_environment
from app.infrastructure.config.layout import layout

prepare_model_environment(layout)

from app.infrastructure.ai.fake_embedding_model import (  # noqa: E402
    FakeEmbeddingModel,
)

__all__ = ["FakeEmbeddingModel"]
