# ADR-003 — As imagens permanecem no sistema de arquivos local

**Status:** Aceito e em vigor
**Origem:** `ARCHITECTURE.md` §21
**Implementado por:** [RFC-021](../rfcs/rfc-021-worker-de-indexacao.md), [RFC-022](../rfcs/rfc-022-dataset-de-demonstracao.md)

---

## Decisão

As imagens **nunca** são copiadas para o banco nem para um diretório gerenciado. O sistema de arquivos do usuário é a fonte de verdade. O banco guarda apenas metadados, embeddings e informação de indexação.

## Justificativa

- **Evita duplicação.** Uma coleção de 100 mil fotos pode ocupar centenas de gigabytes; copiá-la dobraria o custo de armazenamento sem benefício.
- **Reduz armazenamento.** O que o banco guarda por imagem é da ordem de alguns kilobytes (metadados + vetor de 512 floats), não megabytes.
- **Respeita a privacidade do usuário.** Este é um sistema *local-first*: as fotos não saem da máquina, não são enviadas a serviço nenhum, e o banco não se torna um segundo lugar de onde vazá-las.

## Trade-offs

Mudanças no sistema de arquivos precisam ser monitoradas. O banco pode ficar dessincronizado do disco:

- um arquivo **movido ou renomeado** aparece como imagem nova, e a antiga permanece indexada (ver [RFC-021 §3.1](../rfcs/rfc-021-worker-de-indexacao.md));
- um arquivo **removido** continua no índice — não há detecção de exclusão hoje;
- um arquivo **modificado** só é notado na próxima execução do worker.

## Consequências no design

Esta ADR é a razão de várias decisões que, isoladas, pareceriam arbitrárias:

**`ImageId` é derivado do caminho**, via `uuid5` (RFC-021). Se as imagens fossem gerenciadas pelo sistema, um id de banco bastaria. Como não são, a identidade precisa ser recomputável a partir do disco, sem consultar nada.

**A indexação é incremental por metadados de arquivo** (RFC-020): tamanho, mtime e, só se necessário, hash de conteúdo. Todo esse mecanismo existe porque o estado autoritativo está fora do banco.

**O banco só é containerizado; a aplicação, não** (RFC-002 §2). Rodar a aplicação dentro de um container exigiria montar as pastas de fotos do usuário — o que contradiz frontalmente a premissa de privacidade desta ADR.

**Duas fotos idênticas em dois caminhos são duas imagens.** `ContentHasherPort` (RFC-024) diz isso explicitamente: o hash é valor de *detecção de mudança*, nunca de identidade ou deduplicação.
