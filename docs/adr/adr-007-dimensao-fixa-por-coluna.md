# ADR-007 — Dimensão fixa por coluna vetorial

**Status:** Aceito e em vigor — e já cobrado uma vez
**Origem:** `ARCHITECTURE.md` §21, escrita durante o [RFC-017b](../rfcs/rfc-017-modelos-sqlalchemy.md)
**Relacionado:** [ADR-005](adr-005-metadados-de-embedding.md), [RFC-018](../rfcs/rfc-018-migrations-e-indice-hnsw.md), [RFC-023](../rfcs/rfc-023-adaptador-de-embedding-clip.md)

---

## Decisão

Cada modelo de embedding usa uma coluna vetorial **dedicada e de dimensão fixa**. O sistema não suporta guardar embeddings de dimensões diferentes em uma mesma coluna `vector`.

## Justificativa

pgvector exige dimensão fixa por coluna — `vector(512)` aceita exatamente 512 componentes. SigLIP, CLIP e modelos futuros produzem vetores de larguras diferentes, então uma coluna genérica única não conseguiria acomodá-los sem **padding** ou **truncamento**.

Ambos distorcem a similaridade de cosseno:

- **Padding com zeros** altera a norma do vetor e, portanto, todo cosseno calculado contra ele;
- **Truncamento** descarta dimensões que carregam significado, e o número resultante continua parecendo uma similaridade válida.

O modo de falha é o pior possível: nenhum erro, um número plausível, um ranking confiante e sem sentido.

## Consequência

Suportar múltiplos modelos simultaneamente (previsto em `ARCHITECTURE.md` §23) exigiria uma de duas coisas:

1. Uma tabela `Embeddings` por modelo/dimensão; ou
2. Uma constraint escopada por `collection_id` garantindo que todos os embeddings de uma coleção compartilhem `model_name`, `model_version` e `embedding_dimension`.

A implementação atual adota conceitualmente a segunda — mas ver [ADR-005](adr-005-metadados-de-embedding.md): a tabela `Collections` ainda não existe, então a garantia é hoje uma disciplina de operação, não uma constraint.

## Trade-offs

- **Busca entre modelos é impossível.** Comparar um embedding SigLIP com um CLIP não é permitido, e não deveria ser.
- **Migrar uma coleção para um modelo novo exige reindexação completa**, não atualização incremental.

## Como esta ADR se pagou

O projeto começou assumindo SigLIP com 1152 dimensões (ADR-001) e criou `vector(1152)` na migration `999b801e80f4`.

O RFC-023 mediu quatro checkpoints e escolheu CLIP com **512 dimensões**. A consequência foi exatamente a prevista aqui: migration `db526438ced5` alterando a coluna, e **reindexação completa** de tudo.

O ponto não é que a ADR evitou o custo — ela não evitou. O ponto é que o custo era **conhecido antes de ser pago**. A troca de modelo foi uma decisão informada, com um preço já orçado, em vez de uma descoberta no meio da implementação.

## Como a decisão é aplicada no código

```python
# infrastructure/database/models/image_model.py
EMBEDDING_DIMENSION = 512     # constante de módulo, não settings

embedding: Mapped[list[float] | None] = mapped_column(
    Vector(EMBEDDING_DIMENSION), nullable=True
)
```

A largura é **constante de módulo, não `settings.embedding_dimension`**, deliberadamente: é esquema físico fixado por migration, e não pode seguir silenciosamente uma variável de ambiente para longe do que o banco realmente contém.

O teste `test_image_model_embedding_column_matches_configured_dimension` verifica que os dois números continuam concordando. Quem os aproxima é o desenvolvedor, ao escrever a migration — não o import.
