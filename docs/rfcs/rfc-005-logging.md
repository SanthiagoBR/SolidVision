# RFC-005 — Infraestrutura de Logging

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-004 (Settings)
**Bloqueia:** RFC-006, RFC-007, RFC-008, RFC-021
**Commit:** `fb27cbc`
**Última atualização:** 2026-08-24

---

## 1. Contexto

O componente mais importante do sistema — o worker de indexação (RFC-021) — é um processo em lote, longo, sem interface. Quando ele processa 100 mil imagens e 12 falham, a **única** forma de descobrir quais e por quê é o log. `print()` não serve: não tem nível, não tem timestamp, não vai para arquivo, e não sobrevive a um terminal fechado.

## 2. Decisão

Uma fábrica central, `get_logger(name)`, que configura o logger raiz da aplicação **uma vez** e devolve loggers filhos nomeados por módulo. Dois handlers: console e arquivo rotativo.

```python
from app.infrastructure.logging.logger import get_logger
logger = get_logger(__name__)
```

Nenhum módulo chama `logging.basicConfig()`, adiciona handlers, ou instancia `Formatter`.

## 3. O que foi entregue

```
infrastructure/logging/
├── logger.py       # get_logger(), log_exception(), configuração do raiz
├── handlers.py     # build_console_handler(), build_rotating_file_handler()
└── formatters.py   # AppFormatter
```

### 3.1 Configuração idempotente

```python
def _configure_root_logger() -> logging.Logger:
    root_logger = logging.getLogger(_APP_LOGGER_NAME)   # "app"
    if root_logger.handlers:
        return root_logger                              # já configurado

    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.propagate = False
    root_logger.addHandler(build_console_handler())
    root_logger.addHandler(build_rotating_file_handler(...))
    return root_logger
```

O guarda `if root_logger.handlers` é o núcleo deste RFC. `get_logger()` é chamado no topo de dezenas de módulos; sem ele, cada import adicionaria mais um par de handlers, e a mesma linha apareceria duplicada, triplicada, e assim por diante. Esse bug tem sintoma confuso (*"por que cada log aparece 7 vezes?"*) e causa distante do sintoma. O commit original chegou a incluir um script `test_duplicate_handlers.py` justamente para verificar isso.

### 3.2 Hierarquia com `propagate = False`

Loggers são criados como `app.<módulo>`, filhos de `app`. Herdam os handlers do pai, e o nome do módulo aparece em cada linha. `propagate = False` impede que as mensagens subam para o logger raiz do Python — sem isso, qualquer biblioteca que chame `logging.basicConfig()` (SQLAlchemy, uvicorn, transformers) passaria a duplicar as linhas da aplicação com formatação diferente.

### 3.3 Formato

```python
class AppFormatter(logging.Formatter):
    def format(self, record):
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return f"{timestamp} | {record.levelname} | {record.name} | {record.getMessage()}"
```

```
2026-08-23 14:02:11 | INFO | app.infrastructure.workers.indexing_worker | Indexing started
2026-08-23 14:02:11 | ERROR | app.infrastructure.workers.indexing_worker | Failed to index C:/fotos/quebrada.jpg
```

Delimitador `|` porque é raro em mensagens e trivial de fatiar com `cut -d'|'` ou `awk`.

### 3.4 Arquivo rotativo

```python
RotatingFileHandler(
    filename=log_directory / log_filename,
    maxBytes=10 * 1024 * 1024,   # 10 MB
    backupCount=5,               # teto de ~60 MB
    encoding="utf-8",
)
```

Teto obrigatório: uma execução do worker sobre 100 mil imagens emite pelo menos uma linha por arquivo. Sem rotação, um único `application.log` cresceria indefinidamente. `encoding="utf-8"` é explícito porque no Windows o default é a codepage do sistema, que corrompe caminhos com acento — e caminhos de fotos em português têm acento.

### 3.5 Diretório de log resolvido a partir da raiz do projeto

```python
def _resolve_log_directory() -> Path:
    configured = Path(settings.log_directory)
    if configured.is_absolute():
        return configured
    return Path(__file__).resolve().parents[4] / configured
```

Mesmo raciocínio do `.env` no RFC-004: `logs/` relativo ao diretório de trabalho geraria pastas de log diferentes conforme o processo fosse iniciado da raiz, de `backend/`, ou pelo `pytest`. `mkdir(parents=True, exist_ok=True)` roda dentro do builder do handler, então a pasta não precisa existir de antemão.

### 3.6 `log_exception()`

```python
def log_exception(logger, message, exc_info=True) -> None:
    logger.exception(message, exc_info=exc_info)
```

Wrapper fino, mas com propósito: dá **um** nome ao ato de "logar erro com traceback", em vez de deixar cada chamador escolher entre `logger.error(...)`, `logger.error(..., exc_info=True)` e `logger.exception(...)` — três formas com resultados diferentes.

## 4. Notas de projeto

**Logging é Infrastructure, e fica lá.** Domain e Application não logam. O worker (`IndexingWorker.run()`) loga as falhas que o caso de uso lhe *devolve* dentro de um `IndexingSummary`; o caso de uso apenas as coleta. Isso mantém a camada Application testável sem capturar log, e é a razão pela qual `IndexingSummary` existe como valor de retorno em vez de o caso de uso escrever direto no log.

**Formato texto, não JSON.** Este é um sistema local-first, sem agregador de logs. Legibilidade humana no terminal vale mais que parseabilidade por máquina. Se um dia houver coleta centralizada, `AppFormatter` é o único ponto a mudar.

## 5. Testes

Sem arquivo de teste dedicado. As garantias são verificadas indiretamente: se a configuração não fosse idempotente, a duplicação apareceria em qualquer execução do `pytest`, já que a suíte importa dezenas de módulos que chamam `get_logger()`.

## 6. Limitações

- Sem logging estruturado (campos-chave, correlation id).
- Sem `TimedRotatingFileHandler`; a rotação é por tamanho apenas.
- Nível único para toda a aplicação; não é possível pôr apenas o worker em `DEBUG`.
