# RFC-010 — Porta de Repositório de Imagens

**Status:** Implementado
**Sprint:** 2
**Depende de:** RFC-009 (Image, ImageId)
**Bloqueia:** RFC-015 (casos de uso), RFC-016 (DI), RFC-019 (implementações)
**Commit:** `c7813b1` — *RFC_10 Image repository abstraction*
**Última atualização:** 2026-08-24

---

## 1. Contexto e desvio de escopo

O roadmap original chamava este item de "010 Value Objects". Mas os value objects já haviam sido entregues dentro do RFC-009 — `Image` não pode existir sem `ImageId` e `ImagePath`. O escopo real deste RFC passou a ser o próximo item de fato necessário: **a porta de persistência**.

Vale registrar o desvio explicitamente, porque ele é a primeira metade do problema de numeração que os RFCs 011, 012 e 012b terminam de resolver. A pergunta que o roadmap não respondia — *"portas ficam em `domain/ports/` ou em `domain/repositories/`?"* — foi respondida aqui pela implementação: **`domain/repositories/`**, seguindo a convenção já escrita em `AI_Context.md`.

## 2. Decisão

Declarar `ImageRepository` como classe abstrata (`ABC`) dentro do **Domain**, não da Application.

```python
class ImageRepository(ABC):
    @abstractmethod
    def save(self, image: Image) -> None: ...
    @abstractmethod
    def get(self, image_id: ImageId) -> Image | None: ...
    @abstractmethod
    def exists(self, image_id: ImageId) -> bool: ...
    @abstractmethod
    def delete(self, image_id: ImageId) -> None: ...
    @abstractmethod
    def list(self) -> list[Image]: ...
```

### Por que no Domain

Esta é a **Inversão de Dependência** que a arquitetura inteira assume. A regra é: quem *usa* a abstração define a abstração; quem a *implementa* depende dela.

```
Domain define ImageRepository  ←  Infrastructure implementa PostgresImageRepository
      ↑
Application usa ImageRepository
```

O resultado prático: `PostgresImageRepository` importa `app.domain.repositories.image_repository`. O Domain **não importa nada** de `infrastructure`. A seta de dependência aponta para dentro, e trocar PostgreSQL por outra coisa não toca o Domain.

### Por que `ABC` e não `Protocol`

`Protocol` daria tipagem estrutural — uma classe qualquer com os métodos certos seria aceita, sem herança. É mais flexível, e por isso mesmo mais fraco aqui: com `ABC`, esquecer de implementar um método faz a instanciação falhar imediatamente com `TypeError`. Considerando que o contrato cresceu de 5 para 10 métodos ao longo do projeto (RFCs 021, 024, 025), essa falha barulhenta foi útil todas as vezes.

## 3. O que foi entregue

- `backend/app/domain/repositories/image_repository.py`
- `backend/app/domain/repositories/__init__.py`
- `backend/tests/domain/test_image_repository_port.py`

## 4. Notas de projeto

**Assinaturas em tipos de domínio, não em primitivos.** `get(image_id: ImageId)`, não `get(image_id: str)`. A porta fala a linguagem do Domain; converter para `uuid.UUID` é trabalho do adaptador.

**`get()` devolve `Image | None`, não levanta exceção.** "Não encontrado" é um resultado esperado de uma consulta, não um erro. `ImageNotFoundError` existe (RFC-012b) para quando *ausência* é de fato uma violação — decidir isso é responsabilidade do chamador, não do repositório.

**Sem paginação em `list()`.** Adequado ao escopo da Sprint 2. Quando a busca chegou (RFC-025), ela não foi acrescentada a `list()`: ganhou seu próprio método, `search_similar(embedding, limit)`, com contrato próprio de ordenação e limite.

## 5. Como o contrato evoluiu

Este RFC entregou 5 métodos. O contrato atual tem 10 — e cada acréscimo foi justificado por um RFC:

| Método | Origem |
|---|---|
| `save`, `get`, `exists`, `delete`, `list` | **RFC-010** |
| `save_indexed`, `get_index_metadata`, `update_index_metadata` | RFC-021 (indexação incremental) |
| `save_indexed_many`, `get_index_metadata_many` | RFC-024 (lote) |
| `search_similar` | RFC-025 (busca semântica) |

O `save()` original nunca foi removido: continua sendo o contrato *criar-uma-vez* usado por `IndexImageUseCase`.

## 6. Testes

`tests/domain/test_image_repository_port.py` verifica propriedades da **porta**, não de uma implementação:

- `ImageRepository` não pode ser instanciada diretamente;
- uma subclasse incompleta falha na instanciação;
- uma subclasse completa satisfaz o contrato;
- todos os métodos declarados estão marcados como `@abstractmethod`.

## 7. Limitações

- Sem transações explícitas na porta. Atomicidade só aparece no RFC-024, e apenas para `save_indexed_many()`.
- Sem filtros, ordenação ou paginação em `list()`.
- Sem escopo por coleção — decisão reafirmada no RFC-025 §8.
