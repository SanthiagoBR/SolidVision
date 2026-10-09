# RFCs do SolidVision

Registro de decisões de implementação do projeto, um documento por unidade de trabalho entregue. Cada RFC responde às mesmas perguntas: **qual era o problema, o que foi decidido, por que essa alternativa e não as outras, e como sabemos que funciona.**

Os RFCs 001–021 foram escritos retroativamente a partir do código e do histórico do git, e são compactos. Os RFCs 022–026 foram escritos junto com a implementação e carregam as medições completas que sustentam cada decisão. Os RFCs 027–030 foram escritos **antes** da implementação, com todo número marcado `TBM` até ser medido (a convenção de rascunho do [RFC-026](rfc-026-api-de-busca.md)). Os quatro já foram implementados e reescritos: neles não resta nenhum `TBM`, e cada número vem com a escala e as condições em que foi medido.

Documentos relacionados: [ARCHITECTURE.md](../../ARCHITECTURE.md) (arquitetura de referência), [ADRs](../adr/) (decisões arquiteturais), [AI_Context.md](../../AI_Context.md) (convenções operacionais).

---

## Sprint 1 — Fundação

| # | RFC | Entregou | Status |
|---|---|---|---|
| 001 | [Estrutura do Projeto](rfc-001-estrutura-do-projeto.md) | Clean Architecture em 4 camadas, `ARCHITECTURE.md`, `AI_Context.md` | ✅ |
| 002 | [Docker e PostgreSQL com pgvector](rfc-002-docker-e-postgres.md) | Compose com `pgvector/pgvector:pg17`, healthcheck, init da extensão | ✅ |
| 003 | [Ambiente Python e Ferramental](rfc-003-ambiente-python.md) | Python 3.12+, Ruff, Black, `mypy --strict`, pytest com marcador `slow` | ✅ |
| 004 | [Configuração Tipada](rfc-004-configuracao-tipada.md) | `Settings` com pydantic-settings; nada lê `os.environ` fora de `config/` | ✅ |
| 005 | [Logging](rfc-005-logging.md) | `get_logger()` idempotente, console + arquivo rotativo | ✅ |
| 006 | [Fundação SQLAlchemy](rfc-006-fundacao-sqlalchemy.md) | `Base` com convenção de nomes, engine com `pool_pre_ping`, `get_db()` | ✅ |
| 007 | [Migrations com Alembic](rfc-007-alembic.md) | URL vinda do `Settings`, template customizado, `downgrade()` obrigatório | ✅ |
| 008 | [Health API](rfc-008-health-api.md) | `GET /health` com `SELECT 1` real, 503 quando o banco cai | ✅ |

## Sprint 2 — Domínio e Aplicação

| # | RFC | Entregou | Status |
|---|---|---|---|
| 009 | [Fundações do Domain](rfc-009-fundacoes-do-dominio.md) | `Image`, `ImageId`, `ImagePath` — imutáveis, validados no construtor | ✅ |
| 010 | [Porta de Repositório](rfc-010-porta-de-repositorio.md) | `ImageRepository` (ABC) no Domain — a inversão de dependência do projeto | ✅ |
| 011 | [Exceções de Domínio](rfc-011-excecoes-de-dominio.md) | Conflito módulo-vs-pacote; absorvido pelo 012b | ⚠️ Superseded |
| 012 | [Interfaces de Repositório](rfc-012-interfaces-de-repositorio.md) | Duplicava o RFC-010; revertido pelo 012b | ❌ Rejeitado |
| 012b | [Consolidação do Domínio](rfc-012b-consolidacao-do-dominio.md) | Uma coisa, um lugar: exceções como pacote, uma única porta | ✅ |
| 013 | [Porta de Embedding](rfc-013-porta-de-embedding.md) | `EmbeddingModelPort` + `EmbeddingVector` — a fronteira que absorveu a troca de modelo | ✅ |
| 014 | [FakeEmbeddingModel](rfc-014-fake-embedding-model.md) | Duplo determinístico via SHA-256; geometria corrigida no RFC-022 §7.4 | ✅ |
| 015 | [Casos de Uso](rfc-015-casos-de-uso.md) | `IndexImageUseCase`, `SearchImagesUseCase`, teste de arquitetura da camada | ✅ |
| 016 | [Injeção de Dependências](rfc-016-injecao-de-dependencias.md) | Composition root; `InMemoryImageRepository` | ✅ |

> **Sobre os RFCs 010–012b.** O roadmap original e a árvore de arquivos divergiram, e a divergência produziu uma porta duplicada e um conflito de imports. Os três documentos registram isso honestamente em vez de reescrever a história — ver [RFC-012 §4](rfc-012-interfaces-de-repositorio.md) para a lição.

## Sprint 3 — Persistência e Indexação

| # | RFC | Entregou | Status |
|---|---|---|---|
| 017 | [Modelos SQLAlchemy](rfc-017-modelos-sqlalchemy.md) | `ImageModel` separado da entidade; ADR-007 | ✅ |
| 018 | [Migrations e Índice HNSW](rfc-018-migrations-e-indice-hnsw.md) | Tabela `images`, coluna `vector`, HNSW com `vector_cosine_ops` | ✅ |
| 019 | [Repositório PostgreSQL](rfc-019-repositorio-postgresql.md) | `PostgresImageRepository`; fixture `db_session` isolada por SAVEPOINT | ✅ |
| 020 | [Metadados Incrementais](rfc-020-metadados-incrementais.md) | `file_size` + `file_modified_at`; `NULL` significa *desconhecido* | ✅ |
| 021 | [Worker de Indexação](rfc-021-worker-de-indexacao.md) | Identidade determinística por `uuid5`, descoberta em streaming, CLI | ✅ |

## Sprint 4 — IA e Busca

| # | RFC | Entregou | Status |
|---|---|---|---|
| 022 | [Dataset de Demonstração](rfc-022-dataset-de-demonstracao.md) | 45 imagens licenciadas, manifesto, 25 consultas de *ground truth*, casos difíceis gerados | ✅ |
| 023 | [Adaptador de Embedding CLIP](rfc-023-adaptador-de-embedding-clip.md) | *Bake-off* de 4 checkpoints → CLIP LAION ViT-B/32, 512 dim, PT→EN | ✅ |
| 024 | [Pipeline de Embeddings](rfc-024-pipeline-de-embeddings.md) | Batching, hash SHA-256 de conteúdo, escritas em lote, isolamento de erro | ✅ |
| 025 | [Busca Semântica](rfc-025-busca-semantica.md) | `search_similar()` sobre pgvector; Recall@5 84,0% medido | ✅ |
| 026 | [API de Busca](rfc-026-api-de-busca.md) | `GET /api/v1/images/search`; correção do vazamento de sessão | ✅ |

## Sprint 5 — Acervo Real

| # | RFC | Entregou | Status |
|---|---|---|---|
| 027 | [Dispositivos e Identidade de Volume](rfc-027-dispositivos-e-identidade-de-volume.md) | `Device`, GUID de volume, `(device_id, relative_path)`, filtro por dispositivo | ✅ |
| 028 | [Data de Captura e Filtro Temporal](rfc-028-data-de-captura-e-filtro-temporal.md) | `captured_at` via EXIF, `capture_source`, filtro por intervalo | ✅ |
| 029 | [Jobs de Indexação e Indexação Seletiva](rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md) | `indexing_jobs`, `POST /jobs`, executor sondando, cancelamento cooperativo | ✅ |
| 030 | [Acesso ao Arquivo](rfc-030-acesso-ao-arquivo.md) | Caminho na resposta, thumbnails com `ETag`, `POST /images/{id}/reveal` atrás de dois guardas | ✅ |

> **De onde veio a Sprint 5.** Um mockup de interface no Figma desenhou uma barra lateral de HDs, um filtro de datas e uma tela de pastas indexadas — e a pergunta era se aquilo fazia sentido para o projeto. Fazia: três das quatro peças já estavam desenhadas no [ARCHITECTURE.md](../../ARCHITECTURE.md) §15 e nunca construídas. A quarta encontrou um defeito no código entregue: [RFC-027 §2](rfc-027-dispositivos-e-identidade-de-volume.md) mostra que a identidade de uma imagem é derivada do caminho absoluto, e que **a letra de unidade de um HD externo muda sozinha** — reindexando um disco inteiro em silêncio, a ~5 h de inferência por vez.
>
> **A ordem não é arbitrária.** O RFC-027 reescreveu a chave primária de `images`, o que era um `UPDATE` em uma tabela enquanto nada referenciasse `images.id` — e nada referenciava. O RFC-029 preservou essa propriedade de propósito ([§11](rfc-029-jobs-de-indexacao-e-indexacao-seletiva.md)); o RFC-030 a encerrou, ao nomear os arquivos de thumbnail pelo id ([§7.3](rfc-030-acesso-ao-arquivo.md)). A janela para a correção barata ficou aberta exatamente o tempo necessário, e fechou no fim desta sprint, como previsto.

## Sprint 6 — Interface

| # | RFC | Entrega | Status |
|---|---|---|---|
| 031 | [Dispositivos, Volumes e Pastas pela API](rfc-031-dispositivos-e-pastas-pela-api.md) | `GET /devices`, `GET /volumes`, `POST /devices`, `PATCH /devices/{id}`, `GET /devices/{id}/folders`, `GET /capabilities` | ✅ |
| 032 | [Geolocalização e Busca por Proximidade](rfc-032-geolocalizacao-e-busca-por-proximidade.md) | `latitude`/`longitude` via EXIF, `position_source`, filtro por círculo, `GET /images/map`, `exif_backfill` | ✅ |

> **Por que este RFC existe.** A Sprint 5 resolveu tudo o que acontece *depois* que um dispositivo existe, e nada sobre como ele passa a existir para um cliente que não é um terminal. `POST /api/v1/jobs` recebe `{device_id, scopes[]}` e **nenhuma rota devolve um `device_id` ou um `scope` válido** — a rota mais importante da Sprint 5 só é chamável por quem já consultou o banco à mão. A revisão da segunda versão do mockup encontrou o mesmo buraco pelo outro lado: as três telas desenhadas não têm de onde desenhar ([RFC-031 §1](rfc-031-dispositivos-e-pastas-pela-api.md)).
>
> **E por que o RFC-032 é o quarto eixo de busca.** O pedido era *"a pesquisa é muito básica para o acervo em que vai ser utilizado"*, e o eixo que falta é o único que o CLIP não pode aprender: duas fotos aéreas de propriedades rurais a 200 km uma da outra são vizinhas no espaço de embeddings. O [RFC-028 §11](rfc-028-data-de-captura-e-filtro-temporal.md) já havia nomeado o trabalho e a condição para ele existir — *"nada consumiria coordenadas ainda"* —, e o [RFC-032](rfc-032-geolocalizacao-e-busca-por-proximidade.md) entrega o leitor junto com o dado. Uma medição-piloto sobre 40 arquivos reais do acervo precedeu o documento, e já matou uma suposição dele ([§2.3](rfc-032-geolocalizacao-e-busca-por-proximidade.md)).

## Standalone — sem Docker *(proposto)*

Segue o [roadmap sem Docker](ROADMAP-standalone.md).

| # | RFC | Entrega | Status |
|---|---|---|---|
| 033a | [Linha de Base do PostgreSQL Embarcado](rfc-033a-linha-de-base-do-postgres-embarcado.md) | Etapa 0: script do spike reescrito, ADR-008, dev em `pgvector/pgvector:0.8.6-pg16` com `UTF8` e localidade `C` | ✅ |
| 033 | [PostgreSQL Embarcado](rfc-033-postgres-embarcado.md) | Adaptador dono do ciclo de vida do cluster: runtime por lockfile, `initdb` atômico, porta com fallback, adoção, recuperação de encerramento sujo, `run --` até o RFC-034 | 📋 |

---

## Como ler estes documentos

**Se você quer entender a arquitetura**, comece pelo [RFC-001](rfc-001-estrutura-do-projeto.md), depois [RFC-010](rfc-010-porta-de-repositorio.md) e [RFC-013](rfc-013-porta-de-embedding.md) — as duas portas que sustentam tudo.

**Se você quer entender por que o sistema é rápido**, leia [RFC-020](rfc-020-metadados-incrementais.md) (o que não é reprocessado), [RFC-024 §4](rfc-024-pipeline-de-embeddings.md) (economia de 75× via hash) e [RFC-025 §7](rfc-025-busca-semantica.md) (a ordenação fica no banco).

**Se você quer ver medição de verdade**, [RFC-023 §3](rfc-023-adaptador-de-embedding-clip.md) escolheu o modelo por *bake-off* empírico, e [RFC-024 §17](rfc-024-pipeline-de-embeddings.md) tem uma seção inteira sobre hipóteses que os números mataram.

**Se você quer aprender com os erros**, [RFC-012](rfc-012-interfaces-de-repositorio.md) (duplicata silenciosa), [RFC-014 §3.2](rfc-014-fake-embedding-model.md) (um bug que passava em todos os testes), [RFC-026 §7](rfc-026-api-de-busca.md) (um vazamento de sessão que só virou bug sob HTTP) e [RFC-027 §2](rfc-027-dispositivos-e-identidade-de-volume.md) (uma identidade estável que não era).

**Se você quer saber o que vem a seguir**, comece pelos riscos do [RFC-032 §11](rfc-032-geolocalizacao-e-busca-por-proximidade.md): o critério de [§6.1](rfc-032-geolocalizacao-e-busca-por-proximidade.md), aplicado como escrito, não deixou nenhuma mitigação do resultado curto passar — a decisão seguinte é sobre o critério, não sobre o código. Para ver como um RFC escrito antes da implementação envelhece, a Sprint 5 é toda assim: comece pelo [RFC-027 §2](rfc-027-dispositivos-e-identidade-de-volume.md), que é o defeito que ordenou o resto.

## Convenções

- **Status:** ✅ Implementado · 📋 Proposto · ⚠️ Superseded · ❌ Rejeitado
- Um RFC **proposto** não descreve o que foi feito, e sim o que se pretende fazer. Todo número nele é `TBM` — *a medir* — até a implementação escrevê-lo de volta. Um RFC proposto que passe a implementado sem que nenhum `TBM` tenha virado número não foi implementado: foi presumido.
- Um RFC descreve **o que foi decidido e por quê**, não como usar o código. Instruções de uso ficam nos READMEs.
- Números medidos declaram a máquina e as condições em que foram obtidos. Números não medidos são marcados como estimativa.
- Decisões revertidas permanecem documentadas. Reescrever um RFC para esconder um caminho errado apaga a única informação que ele carregava de graça.
