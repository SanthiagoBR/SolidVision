# ADRs — Architecture Decision Records

Decisões estruturais do SolidVision: aquelas que moldam o sistema inteiro e cujo custo de reverter é alto. Um ADR registra a decisão, a justificativa e o **trade-off aceito** — a parte que costuma ser esquecida e que é a única que ajuda quando a decisão é questionada meses depois.

Estes ADRs foram originalmente escritos em inglês dentro de `ARCHITECTURE.md` §4 e §21, e extraídos para cá em arquivos individuais.

| # | ADR | Decisão | Status |
|---|---|---|---|
| 001 | [Modelo de Embedding](adr-001-modelo-de-embedding.md) | ~~SigLIP~~ → **CLIP LAION ViT-B/32**, 512 dim | ⚠️ Substituído pelo [RFC-023](../rfcs/rfc-023-adaptador-de-embedding-clip.md) |
| 002 | [PostgreSQL em vez de banco vetorial](adr-002-postgresql-em-vez-de-banco-vetorial.md) | PostgreSQL + pgvector, não Milvus/Qdrant | ✅ Em vigor |
| 003 | [Imagens no sistema de arquivos](adr-003-imagens-permanecem-no-sistema-de-arquivos.md) | O disco do usuário é a fonte de verdade | ✅ Em vigor |
| 004 | [Indexação separada do HTTP](adr-004-indexacao-separada-do-http.md) | Worker CLI, nunca FastAPI | ✅ Em vigor |
| 005 | [Metadados de embedding](adr-005-metadados-de-embedding.md) | Coleções registram modelo, versão e dimensão | ⚠️ Parcial — falta `Collections` |
| 006 | [Repository Pattern](adr-006-repository-pattern.md) | Todo acesso a persistência passa por uma porta do Domain | ✅ Em vigor |
| 007 | [Dimensão fixa por coluna](adr-007-dimensao-fixa-por-coluna.md) | Um modelo por coluna `vector(N)`; trocar exige reindexar | ✅ Em vigor |

---

## Os três que mais moldaram o código

**[ADR-006](adr-006-repository-pattern.md)** é o que se pagou de forma mais mensurável: a implementação por trás da porta mudou quatro vezes (memória → PostgreSQL → escrita em lote → sessão por requisição) sem que um único caso de uso, teste de Application ou rota precisasse mudar.

**[ADR-007](adr-007-dimensao-fixa-por-coluna.md)** foi cobrado exatamente uma vez, e do jeito previsto: o RFC-023 trocou de modelo, a dimensão caiu de 1152 para 512, e a reindexação completa foi um custo **orçado de antemão** em vez de uma surpresa no meio da implementação.

**[ADR-003](adr-003-imagens-permanecem-no-sistema-de-arquivos.md)** explica decisões que, isoladas, pareceriam arbitrárias: por que `ImageId` é derivado do caminho, por que existe todo o mecanismo de indexação incremental, e por que só o banco é containerizado.

## E um que ainda não fechou

**[ADR-005](adr-005-metadados-de-embedding.md)** está parcialmente implementado. A dimensão é verificada em três lugares, mas `model_name` e `model_version` não são persistidos por coleção porque a tabela `Collections` não existe. A consequência está declarada no próprio documento: trocar por outro modelo de mesma largura e reindexar parcialmente produziria uma tabela com vetores de dois espaços diferentes, e **nada detectaria isso**.

## Relação com os RFCs

Um **ADR** decide *como o sistema é*. Um **[RFC](../rfcs/)** decide *o que construir agora e como*. Um RFC pode revisar um ADR — foi o que o RFC-023 fez com o ADR-001 —, mas quando isso acontece, o ADR é atualizado para registrar a revisão, e não apagado.
