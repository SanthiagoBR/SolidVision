# RFC-007 — Migrations com Alembic

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-004 (Settings), RFC-005 (Logging), RFC-006 (Base/Engine)
**Bloqueia:** RFC-017, RFC-018, RFC-020, RFC-023, RFC-024
**Commit:** `6f94b8a`
**Última atualização:** 2026-08-24

---

## 1. Contexto

O esquema do banco vai mudar muitas vezes ao longo do projeto: criar `images`, adicionar coluna `vector`, adicionar metadados incrementais, mudar a dimensão do vetor de 1152 para 512, adicionar `content_hash`. Cada uma dessas mudanças precisa ser:

- **reproduzível** — o avaliador roda um comando e obtém o esquema correto;
- **versionada** — o histórico de esquema vive no Git, junto do código que o assume;
- **reversível** — cada mudança tem um `downgrade()`.

`Base.metadata.create_all()` não atende a nada disso: ele cria o que falta e ignora o que mudou. Um `ALTER` de dimensão de vetor passaria despercebido.

## 2. Decisão

Adotar **Alembic**, configurado para ler a URL do banco a partir do `Settings` (RFC-004) e os metadados a partir do `Base` do RFC-006 — nunca de um `alembic.ini` com URL hardcoded.

## 3. O que foi entregue

`backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`.

### 3.1 URL vinda do Settings

```python
from app.infrastructure.config.settings import settings
from app.infrastructure.persistence.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata
```

O `alembic.ini` **não** contém credenciais. Elas viveriam em texto plano em um arquivo versionado, e divergiriam do `.env` no primeiro ambiente diferente. Aqui existe uma única fonte de verdade: o `.env` → `Settings` → Alembic.

`target_metadata = Base.metadata` é o que permite `alembic revision --autogenerate` comparar os modelos declarados com o esquema real.

### 3.2 `fileConfig` tolerante a falha

```python
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except KeyError:
        pass
```

O `alembic.ini` deste projeto é mínimo: não contém as seções `[loggers]`/`[handlers]`/`[formatters]` que o template padrão traz, porque logging já é responsabilidade do RFC-005 e duplicá-lo aqui reconfiguraria o logger raiz do Python por baixo da fábrica da aplicação. Sem seções, `fileConfig()` levanta `KeyError`. O `try/except` deixa isso explícito em vez de exigir seções que existiriam só para não quebrar.

### 3.3 Modo offline e online

`run_migrations_offline()` gera SQL sem conectar (`literal_binds=True`) — útil para revisar o que uma migration vai fazer antes de rodá-la. `run_migrations_online()` conecta e executa, usando `poolclass=pool.NullPool`: migrations são um processo curto e de uso único, não há o que reaproveitar em um pool.

### 3.4 Template customizado

`script.py.mako` foi ajustado para gerar migrations já compatíveis com o padrão do projeto (anotações de tipo modernas, `from collections.abc import Sequence`), de modo que arquivos gerados não precisem de retrabalho manual para passar no `ruff`.

## 4. Histórico de migrations (estado atual)

```
9d29f1a527fe  create images table                        (RFC-017/018)
      ↓
999b801e80f4  add embedding vector + HNSW index          (RFC-018)
      ↓
cbd5647f61b7  add incremental image metadata             (RFC-020)
      ↓
db526438ced5  change embedding to 512 dimensions         (RFC-023)
      ↓
26058b9e1d9a  add image content hash                     (RFC-024)
```

## 5. Notas de projeto

**Autogenerate é ponto de partida, não resultado.** O Alembic não detecta mudança de dimensão de `vector`, não gera `CREATE EXTENSION`, e não sabe criar índice HNSW com `vector_cosine_ops`. Todas as migrations de vetor deste projeto foram escritas ou corrigidas à mão (ver RFC-018).

**`downgrade()` é obrigatório.** Toda migration do projeto implementa o caminho de volta. Isso é verificado por teste automatizado a partir do RFC-020: `test_alembic_migrations.py` sobe o histórico até `head`, desce até a base, e sobe de novo.

**`alembic/` está fora do Ruff e do Black** (RFC-003). Arquivos gerados por template não devem ser reformatados a cada geração.

## 6. Comandos

```bash
cd backend
alembic upgrade head                       # aplicar tudo
alembic revision -m "descrição"            # nova migration vazia
alembic revision --autogenerate -m "..."   # a partir dos modelos
alembic downgrade -1                       # desfazer a última
alembic current                            # revisão aplicada
alembic history --verbose                  # histórico
```

## 7. Testes

`tests/infrastructure/test_alembic_migrations.py` (entregue no RFC-020) exercita o histórico completo contra o PostgreSQL real, em ambas as direções.

## 8. Limitações

- Migrations não são executadas automaticamente na subida da API. É um passo manual deliberado: aplicar DDL como efeito colateral de um `import` é indesejável.
- Não há migrations de dados (backfill), apenas de esquema. As colunas adicionadas pelos RFC-020 e RFC-024 são `NULL` para linhas antigas — o que o código trata como *desconhecido*, nunca como *igual* (ver `IndexMetadata.content_hash`).
