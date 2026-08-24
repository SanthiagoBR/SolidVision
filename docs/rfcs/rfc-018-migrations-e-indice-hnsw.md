# RFC-018 — Migrations, `vector(1152)` e Índice HNSW

**Status:** Implementado (dimensão alterada para 512 pelo RFC-023)
**Sprint:** 3
**Depende de:** RFC-007 (Alembic), RFC-017 (ImageModel)
**Bloqueia:** RFC-019, RFC-021, RFC-025
**Commit:** `64eef7a`
**Última atualização:** 2026-08-24

---

## 1. Contexto

O RFC-017 declarou o modelo ORM. Falta materializá-lo: criar a tabela, instalar `pgvector`, adicionar a coluna vetorial e — o ponto central — **criar o índice que torna a busca por similaridade viável**.

## 2. Decisão

Duas migrations encadeadas, escritas à mão:

```
9d29f1a527fe  create images table
      ↓
999b801e80f4  add embedding vector + HNSW index
```

E o registro do **ADR-007** em `ARCHITECTURE.md`, junto com o `docs/adr/architecture/overview.md`.

## 3. O que foi entregue

### 3.1 `9d29f1a527fe` — tabela base

```python
op.create_table(
    "images",
    sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("path", sa.String(), nullable=False, unique=True),
    sa.Column("filename", sa.String(), nullable=False),
    sa.Column("extension", sa.String(), nullable=False),
)
```

### 3.2 `999b801e80f4` — vetor e índice

```python
op.execute("CREATE EXTENSION IF NOT EXISTS vector")

op.add_column("images", sa.Column("embedding", Vector(1152), nullable=True))

op.create_index(
    "ix_images_embedding_hnsw",
    "images",
    ["embedding"],
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
```

## 4. Notas de projeto

### 4.1 Por que HNSW

*Hierarchical Navigable Small World* é um índice **aproximado** (ANN). Sem índice, uma consulta por similaridade faz varredura sequencial: com 100 mil vetores de 512 dimensões, são 51,2 milhões de multiplicações por consulta.

Alternativa considerada e rejeitada: **IVFFlat**. Ele exige que os dados já existam no momento da criação do índice, para calcular os centroides — e um índice construído sobre uma tabela vazia tem qualidade péssima. Como este projeto cria o esquema antes de indexar qualquer coisa, IVFFlat exigiria uma etapa de reconstrução pós-carga. HNSW constrói incrementalmente, à medida que linhas entram. Registrado em `ARCHITECTURE.md` §16.

### 4.2 `vector_cosine_ops` não é opcional

A classe de operadores precisa **casar com a métrica usada na consulta**. `vector_cosine_ops` serve o operador `<=>` (distância de cosseno). Se o índice fosse criado com `vector_l2_ops` e a consulta usasse `<=>`, o PostgreSQL simplesmente **ignoraria o índice** e faria varredura sequencial — sem erro, sem aviso, apenas lento. É a classe de defeito mais cara de diagnosticar: o resultado está certo, só a performance está errada.

### 4.3 `CREATE EXTENSION` dentro da migration

Redundante com o script de init do Docker (RFC-002), e isso é intencional. Um banco provisionado fora do Compose — na avaliação, em CI, em um servidor gerenciado — precisa que `alembic upgrade head` seja suficiente. `IF NOT EXISTS` torna as duas execuções compatíveis.

### 4.4 O Alembic não gera nada disso

`--autogenerate` não detecta `CREATE EXTENSION`, não sabe criar índice com `postgresql_using="hnsw"`, e não percebe mudança de dimensão de um `vector`. Todas as migrations vetoriais deste projeto foram escritas ou corrigidas manualmente — o commit `64eef7a` é literalmente um *"Fix migration 999b801e80f4"*.

### 4.5 1152 → 512

A dimensão 1152 veio da premissa inicial de usar **SigLIP** (`ARCHITECTURE.md`, ADR-001). O RFC-023 fez um *bake-off* empírico entre quatro checkpoints e escolheu **CLIP ViT-B/32 da LAION**, com 512 dimensões — decisão medida, não assumida.

A mudança exigiu a migration `db526438ced5` e **reindexação completa**, exatamente como o ADR-007 previa. O ADR se pagou: a consequência estava documentada antes de ser paga, então a troca foi uma decisão informada em vez de uma surpresa.

### 4.6 Distância vs. similaridade

pgvector devolve **distância**; o domínio expõe **similaridade**. A conversão é `similaridade = 1 - distância`, feita dentro de `PostgresImageRepository.search_similar()` (RFC-025). O índice ordena por distância crescente; o `SearchHit` carrega similaridade decrescente. As duas ordens são a mesma, e a conversão nunca sai do adaptador.

## 5. Testes

Cobertura direta veio no RFC-020, com `tests/infrastructure/test_alembic_migrations.py`, que roda o histórico completo em ambas as direções contra o PostgreSQL real. Nesta RFC, os testes de `test_image_model.py` foram ampliados para verificar a presença e a largura da coluna vetorial.

## 6. Limitações

- Os parâmetros do HNSW (`m`, `ef_construction`) usam os defaults do pgvector. Não foram ajustados nem medidos.
- `ef_search` (parâmetro de consulta que controla o trade-off recall/latência) não é configurável.
- Sem índice em `path`, além do implícito pela constraint `UNIQUE`.
- O RFC-025 §7 mediu que, no corpus de demonstração, o índice **não é usado** — o planejador escolhe varredura sequencial porque a tabela é pequena demais para o índice compensar. O índice existe para escala, e só passa a ser exercitado a partir de dezenas de milhares de linhas.
