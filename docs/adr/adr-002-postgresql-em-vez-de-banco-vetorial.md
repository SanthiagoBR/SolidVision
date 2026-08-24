# ADR-002 — PostgreSQL + pgvector em vez de um banco vetorial dedicado

**Status:** Aceito e em vigor
**Origem:** `ARCHITECTURE.md` §21
**Implementado por:** [RFC-002](../rfcs/rfc-002-docker-e-postgres.md), [RFC-018](../rfcs/rfc-018-migrations-e-indice-hnsw.md), [RFC-025](../rfcs/rfc-025-busca-semantica.md)

---

## Decisão

Usar **PostgreSQL com a extensão pgvector** em vez de um banco vetorial dedicado (Milvus, Qdrant, Weaviate, Pinecone).

## Justificativa

- **Deploy mais simples.** Um container, uma imagem oficial (`pgvector/pgvector:pg17`), nenhum serviço adicional a operar.
- **Um único banco.** Metadados de imagem e vetores vivem na mesma tabela, na mesma transação. Não existe o problema de manter dois armazenamentos sincronizados — a linha e seu vetor são gravados ou não são, juntos.
- **Desempenho suficiente** para coleções de porte médio, que é o alvo declarado do projeto (`ARCHITECTURE.md` §22: 100 mil imagens).

## Trade-offs

- Escalabilidade menor que Milvus ou Qdrant, que são projetados para bilhões de vetores e sharding horizontal.
- Menos opções de índice e de quantização.
- O tuning de ANN no pgvector é mais limitado (`m`, `ef_construction`, `ef_search`).

## Consequências observadas

**A favor, mediada pelo [RFC-025](../rfcs/rfc-025-busca-semantica.md):** a ordenação acontece **dentro do banco**. Com 100 mil vetores de 512 dimensões, trazer tudo para o processo Python para ranquear seria inviável; `ORDER BY embedding <=> :query LIMIT :k` mantém o trabalho onde os dados estão.

**Um efeito medido e inesperado:** no corpus de demonstração (~50 imagens) o planejador do PostgreSQL **ignora o índice HNSW** e faz varredura sequencial — a tabela é pequena demais para o índice compensar. O índice existe para escala, e só passa a ser exercitado a partir de dezenas de milhares de linhas. Um banco vetorial dedicado teria o mesmo comportamento pelo mesmo motivo; a diferença é que aqui foi possível *observar* isso com `EXPLAIN`.

**Um risco operacional que só um banco relacional traz:** o RFC-025 §7.3 mediu, por acidente, uma varredura HNSW devolvendo 1 de 3 linhas vivas. A causa foi autovacuum bloqueado por conexões `idle in transaction` — o vazamento de sessão descrito no [RFC-026 §7](../rfcs/rfc-026-api-de-busca.md). Índices de PostgreSQL dependem de manutenção; isso é parte do custo desta decisão.
