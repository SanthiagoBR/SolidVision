# RFC-004 — Configuração Tipada (Settings)

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-001, RFC-003
**Bloqueia:** RFC-005, RFC-006, RFC-008 — e todo consumidor de configuração
**Commit:** `fb27cbc`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Praticamente todo componente do sistema precisa de configuração: URL do banco, nome do modelo de embedding, extensões suportadas, nível de log, tamanho de lote. A alternativa preguiçosa — `os.environ["DATABASE_URL"]` espalhado pelo código — falha de três formas:

1. **Sem tipo.** `os.environ` devolve `str`. `int(os.environ["BATCH_SIZE"])` explode em tempo de execução, no meio de uma indexação de 100 mil imagens, não na inicialização.
2. **Sem validação.** Uma porta `"70000"` só falha quando o driver do PostgreSQL tenta usá-la.
3. **Sem inventário.** Não existe um lugar onde se veja tudo que o sistema pode configurar.

## 2. Decisão

Uma única classe `Settings`, baseada em **pydantic-settings**, lida a partir do `.env`, exportada como uma instância de módulo. Nenhum código fora de `infrastructure/config/` lê `os.environ`.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[4] / ".env",
        env_file_encoding=PROJECT_ENCODING,
        case_sensitive=False,
        extra="ignore",
    )
    ...

settings = Settings()
```

## 3. O que foi entregue

`backend/app/infrastructure/config/settings.py` e `constants.py`.

### 3.1 Grupos de configuração

| Grupo | Campos |
|---|---|
| Projeto | `project_name`, `project_version`, `environment`, `debug` |
| Banco | `database_host`, `database_port`, `database_name`, `database_user`, `database_password` |
| Embeddings | `embedding_model`, `embedding_dimension`, `device` |
| Indexação | `batch_size`, `metadata_prefetch_size`, `worker_count`, `supported_extensions`, `indexing_root_path` |
| Busca | `top_k_results` |
| API | `warm_up_models` |
| Logging | `log_level`, `log_directory`, `log_filename` |

*(Alguns desses campos foram adicionados por RFCs posteriores — `batch_size`/`metadata_prefetch_size` pelo RFC-024, `top_k_results` ganhou consumidor no RFC-025, `warm_up_models` veio do RFC-026. A estrutura que os acomoda é deste RFC.)*

### 3.2 Validação declarativa

```python
database_port: int = Field(default=5432, ge=1, le=65535)
embedding_dimension: int = Field(default=512, ge=1)
batch_size: int = Field(default=8, ge=1)
```

Uma porta inválida no `.env` faz `Settings()` falhar no import, com mensagem apontando o campo — não seis camadas abaixo, dentro do driver.

### 3.3 Propriedade derivada em vez de campo

```python
@property
def database_url(self) -> str:
    return (
        f"postgresql+psycopg://{self.database_user}:{self.database_password}"
        f"@{self.database_host}:{self.database_port}/{self.database_name}"
    )
```

Deliberadamente **derivada**, não configurável. Se `DATABASE_URL` fosse um campo próprio, existiriam duas fontes de verdade — a URL e as cinco partes — e nada garantiria que concordassem. O Docker Compose (RFC-002) consome as partes; a aplicação consome a URL montada a partir das mesmas partes.

### 3.4 Normalizadores

```python
@field_validator("environment")
def validate_environment(cls, value: str) -> str:
    return value.strip().lower()

@field_validator("supported_extensions")
def validate_supported_extensions(cls, value):
    return tuple(ext.lower() for ext in value)
```

Normalizar na entrada em vez de em cada leitura. `FilesystemImageProvider` compara `path.suffix.lower()` com esse conjunto; se a normalização vivesse no consumidor, cada novo consumidor teria a chance de esquecê-la.

### 3.5 Constantes que não são configuração

`constants.py` guarda o que **não** deve ser sobrescrito por ambiente:

```python
SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp")
DEFAULT_VECTOR_DISTANCE = "cosine"
PROJECT_ENCODING = "utf-8"
```

A distinção é intencional: `supported_extensions` é um *campo* de `Settings` cujo *default* vem daqui. Mudar a métrica de distância, por outro lado, invalidaria o índice HNSW já construído (RFC-018) — não é algo que uma variável de ambiente deva poder fazer.

## 4. Notas de projeto

**`extra="ignore"`.** O `.env` é compartilhado com o Docker Compose, que define variáveis que a aplicação não conhece. Sem `ignore`, o Pydantic rejeitaria o arquivo inteiro por causa de uma chave alheia.

**`case_sensitive=False`.** `DATABASE_HOST` no `.env` mapeia para `database_host` em Python. Mantém a convenção de cada lado (SCREAMING_SNAKE em env, snake_case em Python) sem tradução manual.

**`parents[4]`.** O caminho do `.env` é resolvido a partir do arquivo do próprio módulo, subindo quatro níveis (`config/` → `infrastructure/` → `app/` → `backend/` → raiz). Assim a configuração é encontrada independentemente do diretório de onde o processo foi iniciado — o worker CLI, o `uvicorn` e o `pytest` partem de lugares diferentes.

**Singleton de módulo, e o que ele custa.** `settings = Settings()` é avaliado no import. Isso significa que configuração é lida **uma vez por processo** — barato e previsível — mas também que testes não podem simplesmente alterar variáveis de ambiente e esperar efeito. Onde isso importou, o código passou a **injetar** o valor em vez de ler o singleton: `SearchImagesUseCase` recebe `default_limit`, `IndexOrUpdateImagesUseCase` recebe `batch_size`. A camada Application não importa `settings` — e `tests/application/test_application_architecture.py` verifica isso.

## 5. Testes

Cobertura indireta, via consumidores: `test_sqlalchemy_foundation.py` (URL do banco), `test_health.py` (versão e ambiente na resposta), `test_image_model.py::test_image_model_embedding_column_matches_configured_dimension` (o `embedding_dimension` configurado bate com a coluna física).

## 6. Limitações

- Não há perfis por ambiente (`.env.development` / `.env.production`); existe um único `.env`.
- Segredos ficam em texto plano no `.env`. Aceitável para um sistema local-first; inadequado para deploy multiusuário.
