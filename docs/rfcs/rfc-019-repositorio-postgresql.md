# RFC-019 — Implementação do Repositório PostgreSQL

**Status:** Implementado
**Sprint:** 3
**Depende de:** RFC-010 (porta), RFC-017 (modelo), RFC-018 (esquema)
**Bloqueia:** RFC-020, RFC-021, RFC-024, RFC-025, RFC-026
**Commit:** `d2d5ef0`
**Última atualização:** 2026-08-24

---

## 1. Contexto

A porta `ImageRepository` existe desde o RFC-010 e tem uma implementação em memória desde o RFC-016. Falta a real: a que fala com o PostgreSQL do RFC-002, através do modelo do RFC-017.

## 2. Decisão

`PostgresImageRepository(session)` — recebe a `Session` por construtor, não a cria.

```python
class PostgresImageRepository(ImageRepository):
    def __init__(self, session: Session) -> None:
        self._session = session
```

Quem gerencia o ciclo de vida da sessão é o chamador. Isso mantém o repositório testável (a sessão do teste é uma sessão de teste) e é o que permite ao RFC-026 corrigir o vazamento de conexões trocando **apenas o provider**, sem tocar nesta classe.

## 3. O que foi entregue

Os cinco métodos do contrato original:

```python
def save(self, image: Image) -> None:
    model = ImageModel.from_domain(image)
    self._session.add(model)
    try:
        self._session.commit()
    except IntegrityError as exc:
        self._session.rollback()
        raise ImageAlreadyExistsError() from exc

def get(self, image_id) -> Image | None: ...
def exists(self, image_id) -> bool: ...
def delete(self, image_id) -> None: ...
def list(self) -> list[Image]: ...
```

*(Os métodos `save_indexed`, `save_indexed_many`, `search_similar`, `get_index_metadata*` e `update_index_metadata` vieram dos RFCs 021, 024 e 025.)*

## 4. Notas de projeto

### 4.1 Traduzir erro de banco em erro de domínio

```python
except IntegrityError as exc:
    self._session.rollback()
    raise ImageAlreadyExistsError() from exc
```

O chamador é a camada Application, que não deve conhecer `sqlalchemy.exc`. Se `IntegrityError` vazasse, a Application passaria a depender do SQLAlchemy só para tratar erro — anulando a inversão de dependência inteira.

Duas escolhas dentro dessa tradução:

- **`rollback()` antes do `raise`.** Uma sessão que falhou fica em estado inutilizável; sem o rollback, a próxima operação na mesma sessão falha com uma mensagem que não tem relação com a causa.
- **`from exc`.** Preserva a cadeia de exceções. O traceback continua mostrando qual constraint foi violada — informação que se perde num `raise` sem encadeamento.

### 4.2 Deixar o banco aplicar a unicidade

`save()` não faz `SELECT` antes do `INSERT`. Um "verifica-depois-age" desse tipo tem janela de corrida: dois processos podem ver "não existe" e ambos inserir. A constraint `uq_images_path` (RFC-017) é a única checagem sem corrida, e ela roda dentro do próprio `INSERT`.

### 4.3 `exists()` seleciona só o id

```python
select(ImageModel.id).where(ImageModel.id == image_id.value)
```

Não `select(ImageModel)`. Carregar a entidade inteira — incluindo, mais tarde, um vetor de 512 floats — para responder uma pergunta booleana seria desperdício por linha. Em uma indexação incremental, `exists()` roda uma vez por arquivo.

### 4.4 `delete()` é idempotente

Deletar o que não existe não é erro. Coerente com `get()` devolver `None` em vez de levantar exceção (RFC-010 §4): o repositório relata fatos; decidir se a ausência é um problema é do chamador.

## 5. Isolamento de testes: a fixture `db_session`

Talvez a contribuição mais reutilizada deste RFC. Testes de repositório precisam de banco real — só assim exercitam constraints, `pgvector` e o SQL de fato gerado. Mas não podem deixar lixo no banco de desenvolvimento nem depender uns dos outros.

```python
@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    connection = EngineInstance.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
```

O detalhe que faz isso funcionar é **`join_transaction_mode="create_savepoint"`**.

O código sob teste chama `commit()` de verdade — `save()` comita, e é exatamente esse commit que dispara o `IntegrityError` que queremos testar. Numa sessão comum, esse commit gravaria em definitivo. Com `create_savepoint`, cada `commit()`/`rollback()` do código sob teste opera sobre um **SAVEPOINT aninhado** dentro da transação externa. A transação externa é sempre desfeita no `finally`.

Resultado: o teste vê o comportamento transacional real (inclusive falhas de constraint), e nenhuma linha sobrevive ao teste.

## 6. Testes

`tests/infrastructure/persistence/test_postgres_image_repository.py` — o maior arquivo de teste de infraestrutura do projeto.

| Grupo | Cobre |
|---|---|
| `save` | persiste; caminho duplicado → `ImageAlreadyExistsError`; sessão utilizável após a falha |
| `get` | ida e volta pelo domínio; `None` quando ausente |
| `exists` | verdadeiro/falso |
| `delete` | remove; não falha quando ausente |
| `list` | vazio, um, muitos |
| Tradução | `ImagePath` normalizado sobrevive à ida e volta |

## 7. Limitações (à época deste RFC)

- Sem paginação em `list()`.
- Sem operações em lote — resolvido no RFC-024.
- Sem busca por similaridade — resolvido no RFC-025.
- Sem controle de transação além do commit por operação; cada método comita sozinho.
