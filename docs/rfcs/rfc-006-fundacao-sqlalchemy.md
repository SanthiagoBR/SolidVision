# RFC-006 — Fundação SQLAlchemy (Base, Engine, Session)

**Status:** Implementado
**Sprint:** 1
**Depende de:** RFC-002 (PostgreSQL), RFC-004 (Settings), RFC-005 (Logging)
**Bloqueia:** RFC-007 (Alembic), RFC-008 (Health), RFC-017 (Modelos), RFC-019 (Repositórios)
**Commit:** `322cb36`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Antes de existir qualquer tabela é preciso decidir três coisas que depois ficam caras de mudar: qual `DeclarativeBase` os modelos herdam, como o engine é criado, e como sessões são abertas e fechadas.

A terceira é a que causa mais dano quando errada — e de fato causou: o RFC-026 §7 documenta um vazamento de sessão que só se manifestou quando a busca ganhou uma rota HTTP, meses depois.

## 2. Decisão

Três módulos, um por responsabilidade, sem nenhum modelo ORM ainda:

| Módulo | Responsabilidade |
|---|---|
| `persistence/base.py` | `Base` declarativa + convenção de nomes de constraints |
| `persistence/engine.py` | Engine único do processo |
| `persistence/session.py` | `sessionmaker` + dependência `get_db()` |

## 3. O que foi entregue

### 3.1 `Base` com convenção de nomes

```python
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=naming_convention)
```

Esta é a decisão mais importante do RFC e a mais fácil de subestimar.

Sem convenção de nomes, o PostgreSQL gera nomes automáticos para constraints anônimas. O Alembic então **não consegue removê-las**: `op.drop_constraint()` exige um nome, e o nome que o autocommit gerou não é conhecido nem estável. O resultado é uma migration de `downgrade()` que não funciona — descoberta tipicamente no pior momento possível.

Com a convenção, toda constraint tem nome determinístico derivado da tabela e da coluna. A migration do RFC-020 depende disso diretamente:

```python
op.drop_constraint("file_size_non_negative", "images", type_="check")
```

### 3.2 Engine

```python
EngineInstance: Engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
    pool_pre_ping=True,
)
```

- **`pool_pre_ping=True`** — emite um `SELECT 1` antes de entregar uma conexão do pool. Sem isso, uma conexão que o servidor derrubou (restart do container, timeout de rede) só falha quando a query real é executada, produzindo um erro esporádico e confuso. Custa um round trip; elimina uma classe inteira de flakiness.
- **`echo=settings.debug`** — SQL no log só quando `DEBUG=true`. Em indexação de lote, `echo=True` gera uma linha por statement e domina o log.
- **`future=True`** — estilo SQLAlchemy 2.0.

### 3.3 Sessão

```python
SessionLocal = sessionmaker(
    bind=EngineInstance,
    autoflush=False,
    expire_on_commit=False,
)

def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
```

- **`autoflush=False`** — flush explícito. Em escrita em lote (RFC-024 §7.2) é preciso controlar exatamente quando o SQL vai ao banco; o autoflush dispararia no meio da montagem do lote.
- **`expire_on_commit=False`** — depois de `commit()`, os objetos continuam utilizáveis. Com o default (`True`), ler qualquer atributo após o commit dispara um novo `SELECT`; em um laço de indexação isso multiplica silenciosamente o número de queries.
- **`get_db()` como gerador com `finally`** — o padrão de dependência do FastAPI: a sessão é fechada quando a resposta é enviada, inclusive se o handler lançar exceção.

## 4. Notas de projeto

**Um engine por processo, uma sessão por unidade de trabalho.** Engine é caro (mantém pool de conexões) e thread-safe. `Session` é barata e **não** é segura para uso concorrente. Por isso o engine é um singleton de módulo e a sessão vem de uma factory.

**A dívida que esta fundação preparou — e que só cobrou depois.** `get_db()` existe desde este RFC, mas o RFC-016 (Injeção de Dependências) fez `get_image_repository()` chamar `SessionLocal()` **diretamente**, sem passar pelo `finally`. Enquanto os únicos chamadores eram testes, isso foi inofensivo. Quando o RFC-026 deu uma rota HTTP à busca, cada requisição passou a vazar uma conexão presa em transação aberta — o que, por sua vez, bloqueia o autovacuum e degrada o recall do índice HNSW (ver RFC-025 §7.3 e RFC-026 §7). A correção foi de uma linha: `session: Session = Depends(get_db)`.

Vale registrar o padrão: a fundação estava certa desde o início; o consumidor é que a contornou.

## 5. Testes

`tests/infrastructure/persistence/test_sqlalchemy_foundation.py`

| Teste | Verifica |
|---|---|
| Engine aponta para a URL configurada | `settings.database_url` é de fato usada |
| `SessionLocal` produz `Session` | factory está ligada ao engine |
| `get_db()` fecha a sessão | esgotar o gerador chama `close()` |
| `Base.metadata` carrega a convenção de nomes | constraints terão nome determinístico |

## 6. Limitações

- Tamanho de pool não é configurável; usa os defaults do SQLAlchemy (5 conexões + 10 de overflow). O RFC-026 §7 explica por que esse número importa sob carga.
- Sem suporte a `async`. O sistema é síncrono de ponta a ponta — decisão reafirmada no RFC-026 §9.
