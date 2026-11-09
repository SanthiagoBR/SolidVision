"""RFC-042 nível A: uma matriz aplicada ao vetor da consulta.

O vetor de texto `t` de uma busca, já normalizado, vira `normalize(t + A t)`
antes de ser comparado com as fotos. Os vetores das fotos não são tocados,
então nada no índice muda e nada é reindexado (RFC-042 §9). Uma imagem usada
como consulta (busca por imagem, "achar parecidas") também não é adaptada,
porque `A` foi treinada com texto.

**A matriz é código**, como o léxico (RFC-047 §4). Fica em
`query_adapter.safetensors`, ao lado deste módulo, é versionada com ele e é
lida como dado deste pacote. O cabeçalho dela diz para o que foi treinada:

- `clip_repository` e `clip_revision`: o CLIP do lockfile. Outro checkpoint
  faz outros vetores, sobre os quais esta matriz não significa nada, então a
  carga o recusa.
- `lexicon_sha256`: o léxico com que as buscas do treino foram traduzidas.
  Quem segura este é um teste, não a carga. Mudar o léxico é um commit, e o
  teste falha nesse commit até a matriz ser treinada de novo e medida
  (RFC-042 §16).

`experiments/rfc-042-fine-tuning/export_adapter.py` escreve o arquivo a partir
de uma matriz do `train_adapter.py`.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

import torch
from safetensors.torch import load as load_tensors

from app.infrastructure.ai.model_runtime import CLIP, read_lockfile

ADAPTER_NAME = "query_adapter.safetensors"
MATRIX_KEY = "matrix"


class QueryAdapterError(Exception):
    """O arquivo do adaptador não é um que esta versão do código possa usar."""


def lexicon_digest(content: bytes) -> str:
    """O sha256 do léxico, com fins de linha normalizados, como o cabeçalho o guarda."""
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def header_metadata(content: bytes) -> dict[str, str]:
    """O `__metadata__` de um arquivo safetensors.

    O arquivo começa com o tamanho do cabeçalho (8 bytes, little-endian) e
    depois o próprio cabeçalho, que é JSON.
    """
    (length,) = struct.unpack("<Q", content[:8])
    header = json.loads(content[8 : 8 + length])
    return {str(k): str(v) for k, v in header.get("__metadata__", {}).items()}


class QueryAdapter:
    """A matriz do nível A, conferida com o CLIP para o qual foi treinada."""

    def __init__(self, matrix: torch.Tensor, metadata: Mapping[str, str]) -> None:
        if (
            matrix.ndim != 2
            or matrix.shape[0] != matrix.shape[1]
            or matrix.dtype != torch.float32
        ):
            raise QueryAdapterError(
                f"the adapter must be a square float32 matrix, not"
                f" {matrix.dtype} {tuple(matrix.shape)}"
            )
        self.matrix = matrix
        self.metadata = dict(metadata)

    @property
    def dimension(self) -> int:
        return int(self.matrix.shape[0])

    @classmethod
    def load(cls, path: Path | None = None) -> QueryAdapter:
        """O adaptador em `path` ou, por padrão, o desta versão."""
        if path is None:
            source = resources.files("app.infrastructure.ai") / ADAPTER_NAME
            name, content = ADAPTER_NAME, source.read_bytes()
        else:
            name, content = path.name, path.read_bytes()
        metadata = header_metadata(content)
        clip = read_lockfile().models[CLIP]
        trained_for = (metadata.get("clip_repository"), metadata.get("clip_revision"))
        if trained_for != (clip.repository, clip.revision):
            raise QueryAdapterError(
                f"{name} was trained for CLIP {trained_for[0]} at {trained_for[1]},"
                f" and the lockfile's is {clip.repository} at {clip.revision}:"
                " train it again (RFC-042 section 16)"
            )
        matrix = load_tensors(content).get(MATRIX_KEY)
        if matrix is None:
            raise QueryAdapterError(f"{name} has no tensor named {MATRIX_KEY!r}")
        return cls(matrix, metadata)

    def apply(self, vectors: torch.Tensor) -> torch.Tensor:
        """`normalize(t + t A^T)` por linha, o que `train_adapter.adapt` treinou."""
        matrix = self.matrix.to(vectors.device)
        return torch.nn.functional.normalize(
            vectors + vectors @ matrix.T, p=2.0, dim=-1
        )
