# RFC-013 — Porta de Embedding + EmbeddingVector

**Status:** Implementado
**Sprint:** 2
**Depende de:** RFC-009, RFC-012b
**Bloqueia:** RFC-014 (fake), RFC-015 (casos de uso), RFC-023 (adaptador CLIP)
**Commit:** `0be0a37`
**Última atualização:** 2026-08-24

---

## 1. Contexto

O sistema precisa transformar imagens e texto em vetores. Fazer isso exige Torch, Transformers e um checkpoint de 600 MB. Nada disso pode aparecer no Domain ou na Application — é regra explícita de `AI_Context.md`:

> *Application code must never import HuggingFace, Torch or Transformers directly.*

Portanto, antes de existir qualquer modelo real, precisa existir o **contrato** por trás do qual ele vai se esconder.

## 2. Decisão

Duas peças no Domain:

- `EmbeddingVector` — value object imutável que representa um vetor semântico;
- `EmbeddingModelPort` — ABC com `encode_image()` e `encode_text()`.

### 2.1 Por que em `domain/services/` e não em `application/ports/`

O scaffold do RFC-001 havia criado `application/ports/embedding_model_port.py`. Este RFC decidiu contra, e o RFC-015 apagou o placeholder.

O critério, agora registrado em `AI_Context.md`:

> *Repositories persist/retrieve existing entities; services represent behavior or transformation contracts.*

Gerar um embedding é uma **transformação de dados de domínio** — recebe `Image`, devolve `EmbeddingVector`, ambos tipos do Domain. Um contrato cujas duas pontas são de domínio pertence ao Domain. Colocá-lo na Application forçaria o Domain a depender da camada acima para descrever seu próprio vocabulário.

## 3. O que foi entregue

### 3.1 `EmbeddingVector`

```python
@dataclass(frozen=True)
class EmbeddingVector:
    values: tuple[float, ...]

    def __init__(self, values: Iterable[float]) -> None:
        object.__setattr__(self, "values", self._normalize(values))

    @staticmethod
    def _normalize(values: Iterable[float]) -> tuple[float, ...]:
        normalized = tuple(float(v) for v in values)
        if not normalized:
            raise InvalidEmbeddingVectorError("Embedding vector cannot be empty")
        return normalized
```

**`tuple`, não `list`.** Imutabilidade real: um `list` dentro de uma dataclass congelada continua mutável, e o objeto deixa de ser hasheável.

**Aceita qualquer `Iterable[float]`.** Adaptadores produzem `numpy.ndarray` ou `torch.Tensor`; o construtor consome sem que o Domain conheça nenhum dos dois. `float(value)` normaliza `numpy.float32` para `float` de Python — o que, na prática, é a fronteira onde tipos de biblioteca de IA param.

**Rejeita vazio, mas não valida dimensão.** Deliberado: o Domain não sabe qual modelo está em uso, e a dimensão é decisão de configuração/persistência. A validação de dimensão existe — no RFC-025, na fronteira do repositório, com `EmbeddingDimensionMismatchError` — que é onde ela pode ser comparada contra o que o índice realmente contém.

### 3.2 `EmbeddingModelPort`

```python
class EmbeddingModelPort(ABC):
    @abstractmethod
    def encode_image(self, image: Image) -> EmbeddingVector: ...

    @abstractmethod
    def encode_text(self, text: str) -> EmbeddingVector: ...
```

Dois métodos, uma propriedade implícita e essencial: **ambos devolvem vetores do mesmo espaço**. É isso que permite comparar o embedding de "gato dormindo" com o embedding de uma foto. O contrato não pode expressar essa garantia em tipos — é obrigação do implementador, e o RFC-023 §6 mostra como é fácil violá-la usando a saída errada do CLIP.

**A porta é read-only.** A docstring exige que implementações tratem `Image` e `text` como imutáveis. Um adaptador que alterasse a entidade recebida introduziria efeito colateral em uma camada que a arquitetura declara pura.

### 3.3 Extensão posterior: `encode_images()`

O RFC-024 acrescentou um terceiro método — **concreto, não abstrato**:

```python
def encode_images(self, images: Sequence[Image]) -> list[EmbeddingVector]:
    return [self.encode_image(image) for image in images]
```

Uma implementação que sabe processar em lote sobrescreve; uma que não sabe herda o laço e continua correta. A alternativa considerada e rejeitada era um contrato opcional separado, sondado com `isinstance` na Application — o que seria um teste de tipo fazendo o papel de um contrato. Ver RFC-024 §5.

### 3.4 `InvalidEmbeddingVectorError`

Acrescentada à hierarquia do RFC-012b, herdando de `DomainError` — portanto já coberta pelo handler HTTP genérico do RFC-026 §8.

## 4. Notas de projeto

**A porta é o que torna o `bake-off` do RFC-023 possível.** Comparar CLIP contra SigLIP, em quatro checkpoints, exigiu trocar a implementação por trás desta interface sem tocar em caso de uso, repositório ou rota. Se `SearchImagesUseCase` importasse `transformers`, o experimento teria custado uma refatoração em vez de uma troca de construtor.

**A porta sobreviveu a uma mudança de dimensão.** O projeto começou assumindo SigLIP com 1152 dimensões e terminou com CLIP de 512 (RFC-023). Nenhuma linha de `EmbeddingModelPort` ou de `EmbeddingVector` mudou.

## 5. Testes

| Arquivo | Cobre |
|---|---|
| `tests/domain/test_embedding_vector.py` | imutabilidade, aceita iteráveis diversos, converte para `float`, rejeita vazio, igualdade por valor |
| `tests/domain/test_embedding_model_port.py` | ABC não instanciável, subclasse incompleta falha, subclasse completa funciona, `encode_images()` default delega a `encode_image()` |

## 6. Limitações

- A porta não expõe `model_name`, `model_version` nem `dimension`. `ARCHITECTURE.md` §11 prevê metadados de modelo por coleção; hoje isso vive apenas em `Settings`.
- Não há contrato para embedding em lote de **texto** — só de imagens (`encode_images`). A busca encoda uma consulta por vez.
