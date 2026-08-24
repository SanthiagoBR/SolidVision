# RFC-002 — Docker e PostgreSQL com pgvector

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-001
**Bloqueia:** RFC-006 (SQLAlchemy), RFC-007 (Alembic), RFC-008 (Health API)
**Commit:** `fb27cbc`
**Última atualização:** 2026-08-24

---

## 1. Contexto

O projeto precisa de PostgreSQL **com a extensão `pgvector`**. Um PostgreSQL comum não serve: sem `pgvector` não existe o tipo `vector`, nem o operador de distância `<=>`, nem índice HNSW — ou seja, não existe busca semântica.

Instalar `pgvector` manualmente em uma instalação local do PostgreSQL exige compilar uma extensão C contra os headers da versão correta do servidor. Isso é reprodutível em uma máquina e frágil em três — exatamente o cenário de um projeto acadêmico avaliado em outro computador.

## 2. Decisão

Subir o banco via **Docker Compose**, usando a imagem oficial `pgvector/pgvector:pg17`, que já traz a extensão compilada.

Apenas o **banco** é containerizado. A aplicação Python roda no host, em um virtualenv (RFC-003). A escolha é deliberada:

- O ciclo editar → rodar teste fica imediato, sem rebuild de imagem.
- O modelo CLIP (RFC-023) baixa ~600 MB de checkpoint; o cache do Hugging Face no host sobrevive entre execuções sem volume extra.
- A indexação lê o **sistema de arquivos local do usuário** (`ARCHITECTURE.md` — *local-first*). Dentro de um container isso exigiria montar as pastas de fotos do usuário, o que contradiz a premissa de privacidade do projeto.

## 3. O que foi entregue

**`docker-compose.yml`**

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    container_name: solidvision-postgres
    restart: unless-stopped
    env_file: [.env]
    environment:
      POSTGRES_DB: ${DATABASE_NAME}
      POSTGRES_USER: ${DATABASE_USER}
      POSTGRES_PASSWORD: ${DATABASE_PASSWORD}
    ports: ["${DATABASE_PORT:-5432}:5432"]
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./docker/postgres/init:/docker-entrypoint-initdb.d
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DATABASE_USER} -d ${DATABASE_NAME}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
```

**`docker/postgres/init/init-pgvector.sql`**

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

## 4. Notas de projeto

**Por que um script de init e não só a imagem.** A imagem `pgvector/pgvector` *disponibiliza* a extensão, mas não a *cria* no banco. `CREATE EXTENSION` precisa ser executado uma vez por banco. Colocá-lo em `/docker-entrypoint-initdb.d` faz o PostgreSQL executá-lo automaticamente na primeira inicialização do volume.

`IF NOT EXISTS` torna o script idempotente. A migration `999b801e80f4` (RFC-018) repete o mesmo `CREATE EXTENSION`, para que um banco provisionado fora do Compose — na avaliação, em CI — também funcione. As duas execuções não conflitam.

**Credenciais vêm do `.env`, nunca do arquivo Compose.** `env_file: [.env]` mais interpolação `${...}`: o mesmo arquivo que a aplicação lê (RFC-004) configura o container. Não existe uma segunda fonte de verdade para senha ou nome de banco.

**Healthcheck com `pg_isready`.** Sem ele, `docker compose up -d` retorna assim que o processo sobe, e um `alembic upgrade head` imediatamente depois falha com *connection refused* — o PostgreSQL ainda está executando o init. O healthcheck dá ao Compose um sinal real de prontidão.

**`start_period: 10s`.** A primeira subida executa os scripts de init e é sensivelmente mais lenta que as seguintes. Sem esse período de carência, as tentativas iniciais contariam como falhas e o container seria marcado como *unhealthy* antes de terminar de nascer.

**Volume nomeado (`postgres_data`).** Os dados sobrevivem a `docker compose down`. Para reindexar do zero: `docker compose down -v`.

## 5. Como usar

```bash
cp .env.example .env
docker compose up -d
docker compose ps          # confirmar "healthy"
```

## 6. Testes

Nenhum teste automatizado cobre o Compose diretamente. A verificação é indireta e forte:

- `tests/presentation/test_health.py` (RFC-008) exercita conexão real e falha de conexão.
- `tests/infrastructure/test_alembic_migrations.py` (RFC-020) roda o histórico completo de migrations contra o banco real, o que só passa se `pgvector` estiver instalada.

## 7. Limitações

- Não há container para a aplicação, nem Dockerfile de produção. Empacotamento para deploy está fora do escopo.
- Não há serviço separado para o worker de indexação; ele roda como processo CLI no host (RFC-021).
