# ADR-005 — Armazenar metadados do modelo de embedding

**Status:** Aceito — parcialmente implementado
**Origem:** `ARCHITECTURE.md` §21
**Relacionado:** [ADR-007](adr-007-dimensao-fixa-por-coluna.md), [RFC-023](../rfcs/rfc-023-adaptador-de-embedding-clip.md)

---

## Decisão

Embeddings gerados por modelos diferentes são **incompatíveis** — ocupam espaços vetoriais distintos, e a similaridade de cosseno entre vetores de espaços diferentes não significa nada.

Portanto, cada coleção deve registrar:

- `model_name`
- `model_version`
- `embedding_dimension`

garantindo que uma coleção seja internamente consistente.

## Justificativa

Comparar um embedding de CLIP com um de SigLIP não produz um resultado ruim — produz um resultado **sem sentido**, com aparência de resultado válido. Um número entre -1 e 1 sai da conta, o ranking ordena, e nada indica que a comparação era inválida. Sem metadados registrados, não há como detectar essa condição depois do fato.

## Estado atual da implementação

**Implementado:** os três valores existem em `Settings` (`embedding_model`, `embedding_dimension`) e a dimensão é fixada fisicamente pela coluna `vector(512)`, com verificação em tempo de execução:

- `PostgresImageRepository._require_indexed_dimension()` rejeita uma consulta de largura errada com `EmbeddingDimensionMismatchError`;
- `InMemoryImageRepository` faz a mesma checagem, contra a dimensão configurada, para que o duplo não seja mais permissivo que o banco;
- o teste `test_image_model_embedding_column_matches_configured_dimension` trava a concordância entre configuração e esquema.

**Não implementado:** não existe tabela `Collections`, e portanto os metadados **não são persistidos por coleção**. Hoje eles existem apenas como configuração de processo. A consequência prática: se alguém trocar `EMBEDDING_MODEL` no `.env` para outro modelo de 512 dimensões e reindexar parcialmente, a tabela conteria vetores de dois espaços diferentes e **nada detectaria isso** — a checagem de dimensão passaria, porque a largura é a mesma.

## Consequência

Trocar o modelo de embedding exige **reindexação completa** da coleção, e essa exigência é hoje uma disciplina do operador, não uma garantia do sistema.

Foi exatamente o que aconteceu no RFC-023 (SigLIP 1152 → CLIP 512): a mudança de dimensão *forçou* a reindexação, porque a coluna física não aceitaria os vetores antigos. Uma troca entre dois modelos de mesma largura não teria essa proteção acidental.

Fechar essa lacuna — a tabela `Collections` de `ARCHITECTURE.md` §15, com constraint de `model_name`/`model_version`/`embedding_dimension` — é trabalho futuro.
