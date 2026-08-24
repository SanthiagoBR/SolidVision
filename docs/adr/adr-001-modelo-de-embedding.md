# ADR-001 — Modelo de Embedding Padrão

**Status:** ⚠️ Substituído pelo [RFC-023](../rfcs/rfc-023-adaptador-de-embedding-clip.md)
**Data original:** Sprint 1 (`ARCHITECTURE.md` §4)
**Revisto em:** 2026-08-21 (RFC-023)

---

## Decisão original

Usar **SigLIP** como modelo de embedding padrão.

## Justificativa original

SigLIP oferece alinhamento semântico entre imagem e linguagem natural mais forte que a arquitetura CLIP original, especialmente em recuperação *zero-shot*. Sua integração com o ecossistema HuggingFace simplifica o deploy e a experimentação futura.

## Trade-offs previstos

- Inferência um pouco mais lenta que CLIP.
- Maior consumo de memória durante a geração de embeddings.
- Exige reindexação completa se for substituído por outro modelo.

---

## Revisão — o que a medição mostrou

O RFC-023 executou um *bake-off* empírico entre quatro checkpoints (CLIP e SigLIP, em 512 e 768 dimensões), sobre o corpus de demonstração do RFC-022 e um conjunto de 25 consultas.

**Resultado: CLIP venceu.** O checkpoint escolhido foi `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`, com **512 dimensões**.

A justificativa completa, incluindo as métricas por checkpoint, o template de prompt e o tratamento de consultas em português, está no [RFC-023 §3 e §4](../rfcs/rfc-023-adaptador-de-embedding-clip.md).

## Consequências da revisão

- A dimensão do vetor mudou de **1152 → 512**, exigindo a migration `db526438ced5` e **reindexação completa** — exatamente o trade-off que esta ADR e o [ADR-007](adr-007-dimensao-fixa-por-coluna.md) já previam.
- Nenhuma linha de `EmbeddingModelPort` ou de `EmbeddingVector` precisou mudar. A porta do [RFC-013](../rfcs/rfc-013-porta-de-embedding.md) absorveu a troca inteira.
- `AI_Context.md` ainda cita "SigLIP" como default em alguns trechos; o código e a configuração (`.env.example`, `Settings.embedding_model`) são a fonte de verdade.

## Lição

A decisão original foi tomada por reputação da arquitetura, não por medição no domínio do projeto. A decisão revista foi tomada por medição. O custo de estar errado foi baixo **porque a abstração estava certa** — trocar o modelo foi trocar uma classe atrás de uma interface.
