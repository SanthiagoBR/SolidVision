# ADR-004 — Indexação separada das requisições HTTP

**Status:** Aceito e em vigor
**Origem:** `ARCHITECTURE.md` §21
**Implementado por:** [RFC-021](../rfcs/rfc-021-worker-de-indexacao.md), [RFC-024](../rfcs/rfc-024-pipeline-de-embeddings.md), [RFC-026](../rfcs/rfc-026-api-de-busca.md)

---

## Decisão

A indexação é executada por um **Worker** — um processo CLI próprio — e **nunca** pelo FastAPI.

```bash
python -m app.infrastructure.workers.indexing_worker --root PATH
```

## Justificativa

- **Indexação é longa.** Uma coleção de 100 mil imagens leva horas. Nenhum cliente HTTP espera isso.
- **Pode falhar de forma independente.** Um arquivo corrompido, um disco cheio, um modelo que não carrega — nada disso deve afetar a disponibilidade da API de busca.
- **Pode ser retomada depois.** Um processo em lote pode ser interrompido e reiniciado; uma requisição, não.
- **Mantém a API responsiva.** Se a indexação rodasse no mesmo processo, a inferência do modelo — que é intensiva em CPU — competiria com as consultas de busca.

## Consequências no design

**O worker monta seu próprio grafo de dependências.** O composition root do RFC-016 usa o `Depends` do FastAPI, que não existe fora do HTTP. Por isso `IndexingWorker.main()` compõe suas dependências à mão — e por isso `get_embedding_model()` **não pode ganhar parâmetros**: o CLI o chama como função de zero argumentos (ver [RFC-016 §5.2](../rfcs/rfc-016-injecao-de-dependencias.md)).

**Não existe rota HTTP de indexação.** `get_index_image_use_case()` existe no composition root e não é consumido por rota nenhuma. Está lá para que o caso de uso permaneça montável e testável, não porque haverá um `POST /index`.

**A API só lê.** O RFC-026 §4.1 lista explicitamente o que a rota de busca é proibida de fazer, e escrever no índice é o primeiro item.

**O worker devolve um `IndexingSummary`**, não um código de saída. Contadores, tempos e falhas são objeto — o que permite que testes e benchmarks façam asserção sobre a execução em vez de parsear log.

## Trade-off aceito

Não há orquestração: o worker é disparado manualmente. Não há agendador, não há observação do sistema de arquivos, e `worker_count` existe em `Settings` sem consumidor. Paralelismo e agendamento são trabalho futuro registrado no RFC-024 §21.
