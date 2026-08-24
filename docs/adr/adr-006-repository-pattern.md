# ADR-006 — Repository Pattern

**Status:** Aceito e em vigor
**Origem:** `ARCHITECTURE.md` §21
**Implementado por:** [RFC-010](../rfcs/rfc-010-porta-de-repositorio.md), [RFC-016](../rfcs/rfc-016-injecao-de-dependencias.md), [RFC-019](../rfcs/rfc-019-repositorio-postgresql.md)

---

## Decisão

Todo acesso a persistência passa por uma abstração de repositório declarada no **Domain**. Casos de uso dependem da abstração; implementações concretas dependem dela também.

```
Domain define ImageRepository  ←  Infrastructure implementa PostgresImageRepository
      ↑                                                     InMemoryImageRepository
Application usa ImageRepository
```

## Justificativa

- A lógica de negócio fica independente de banco de dados.
- Migrações futuras (trocar PostgreSQL por outra coisa) ficam viáveis.
- Testes de Application rodam sem infraestrutura.

## O que isso comprou, concretamente

Esta é a ADR com o retorno mais fácil de demonstrar. Ao longo do projeto, a implementação por trás da porta mudou quatro vezes — e nenhum caso de uso, nenhum teste de Application e nenhuma rota precisou mudar junto:

| RFC | Mudança na implementação |
|---|---|
| 016 | `InMemoryImageRepository` (listas e dicionários) |
| 019 | `PostgresImageRepository` (SQLAlchemy) |
| 024 | escrita em lote atômica (`save_indexed_many`) |
| 026 | sessão por requisição via `Depends(get_db)` |

E três implementações coexistem hoje, submetidas ao **mesmo teste de contrato** (`tests/infrastructure/persistence/test_search_similar_contract.py`): a de PostgreSQL, a em memória, e o duplo de `tests/application/fakes.py`.

## A regra que sustenta isso

> *Never query pgvector directly from Use Cases.* — `AI_Context.md`

O `SearchImagesUseCase` (RFC-025) não conhece cosseno, `ORDER BY`, `LIMIT` nem HNSW. Ele encoda a consulta e chama `search_similar(embedding, limit)`. A ordenação pertence ao repositório — e a docstring da porta é explícita: **o chamador não deve reordenar o resultado**, porque a implementação ranqueia contra o que ela armazena, e reordenar em Python discordaria dela nos empates.

## Trade-offs

**A porta cresce.** Começou com 5 métodos (RFC-010) e tem 10. Cada acréscimo obriga **todas** as implementações a acompanharem — inclusive as de teste. Usar `ABC` em vez de `Protocol` torna isso uma falha alta (`TypeError` na instanciação) em vez de um erro silencioso, o que foi útil nas três vezes em que o contrato cresceu.

**Um duplo pode ser mais permissivo que o real, e isso é perigoso.** Documentado em vários pontos do código: `InMemoryImageRepository.save_indexed_many()` implementa atomicidade **à mão** (aplica em cópias e troca no fim) especificamente para não ser mais tolerante que uma transação de banco; `_cosine_similarity()` valida comprimentos porque `zip` truncaria silenciosamente e devolveria um número plausível calculado sobre 3 de 512 dimensões.

O teste de contrato compartilhado existe justamente para que essa divergência não se instale.
