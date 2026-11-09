"""Modelo de embedding fake e determinístico para desenvolvimento e testes."""

from __future__ import annotations

import hashlib
import math

from app.domain.entities.image import Image
from app.domain.services.embedding_model_port import EmbeddingModelPort
from app.domain.value_objects.embedding_vector import EmbeddingVector
from app.infrastructure.config.settings import settings


class FakeEmbeddingModel(EmbeddingModelPort):
    """Adaptador de embedding determinístico que não usa biblioteca de IA nenhuma.

    Produz vetores unitários centrados na média e normalizados em L2, para
    que a similaridade de cosseno entre duas sementes *diferentes* se espalhe
    por uma faixa realista, como num espaço de embedding de verdade. Uma
    versão anterior somava, em vez disso, um termo posicional que crescia
    linearmente com o índice da dimensão, chegando a ~144 na última
    dimensão, contra um termo de conteúdo confinado a (0, 1] -- todo vetor
    era, na prática, a mesma rampa mais uma perturbação desprezível, então a
    similaridade de cosseno entre duas imagens quaisquer era ~1,0, fosse
    qual fosse o conteúdo. Isso tornava o fake inútil para qualquer coisa
    além de testes do encanamento do pipeline: um benchmark de recall do
    HNSW rodado contra ele teria medido o recall sobre vetores
    indistinguíveis (ver RFC-022 §7.4). O *conteúdo* do vetor continua sem
    significado semântico -- este não é um modelo de embedding de verdade --,
    mas a geometria dele não derrota mais a busca por similaridade de
    graça.
    """

    def encode_image(
        self, image: Image, content: bytes | None = None
    ) -> EmbeddingVector:
        """Deriva um vetor do id e do caminho da imagem; `content` é ignorado.

        Ignorado de propósito (RFC-045 §5.3): o fake não lê arquivo nenhum,
        então os bytes que a leitura antecipada entrega não têm o que mudar,
        e um vetor que dependesse deles seria diferente entre uma execução
        com a leitura antecipada e outra sem.
        """
        seed = f"image::{image.id}::{image.relative_path}".encode()
        return self._build_embedding(seed)

    def encode_text(self, text: str) -> EmbeddingVector:
        seed = f"text::{text}".encode()
        return self._build_embedding(seed)

    def _build_embedding(self, seed: bytes) -> EmbeddingVector:
        raw_values = self._deterministic_floats(seed, settings.embedding_dimension)

        mean = sum(raw_values) / len(raw_values)
        centered = [value - mean for value in raw_values]

        norm = math.sqrt(sum(value * value for value in centered))
        normalized = [value / norm for value in centered]

        return EmbeddingVector(normalized)

    @staticmethod
    def _deterministic_floats(seed: bytes, count: int) -> list[float]:
        """Expande `seed` em `count` floats reproduzíveis espalhados por [-1, 1).

        Usa SHA-256 em modo contador, e não o `hash()` embutido do Python, que
        recebe um sal por processo via `PYTHONHASHSEED` e por isso não é
        reproduzível entre execuções, processos ou máquinas -- um requisito
        rígido aqui, já que a mesma imagem ou o mesmo texto têm de produzir
        sempre o mesmo embedding (ARCHITECTURE.md §20).
        """
        values: list[float] = []
        counter = 0
        while len(values) < count:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for byte_value in block:
                values.append((byte_value / 127.5) - 1.0)
                if len(values) == count:
                    break
            counter += 1
        return values
