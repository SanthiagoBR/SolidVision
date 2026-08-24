# RFC-016 — Injeção de Dependências (Fakes / InMemory)

**Status:** Implementado (evoluído pelos RFC-019, RFC-023 e RFC-026)
**Sprint:** 2
**Depende de:** RFC-010, RFC-013, RFC-014, RFC-015
**Bloqueia:** RFC-019, RFC-026
**Commit:** `30adf1c`
**Última atualização:** 2026-08-24

---

## 1. Contexto

Os casos de uso do RFC-015 recebem portas por construtor. Alguém precisa **escolher as implementações concretas e montá-los**. Esse alguém é o *composition root*: o único módulo autorizado a conhecer todas as classes concretas ao mesmo tempo.

No fim da Sprint 2 ainda não existiam nem `PostgresImageRepository` (RFC-019) nem o adaptador CLIP (RFC-023). Este RFC monta o grafo com o que existe — `InMemoryImageRepository` e `FakeEmbeddingModel` — para que a fiação esteja pronta e testada quando as implementações reais chegarem.

## 2. Decisão

`app/presentation/dependencies/__init__.py` como composition root, expondo *providers* compatíveis com o sistema `Depends` do FastAPI.

```python
def get_image_repository(...) -> ImageRepository: ...
def get_embedding_model() -> EmbeddingModelPort: ...
def get_index_image_use_case(...) -> IndexImageUseCase: ...
def get_search_images_use_case(...) -> SearchImagesUseCase: ...
```

Todos anotados com o tipo da **porta**, nunca com a classe concreta. Um consumidor que declara `Depends(get_image_repository)` recebe algo tipado como `ImageRepository` — o que impede que ele acidentalmente use um método específico do PostgreSQL.

## 3. O que foi entregue

- `infrastructure/persistence/in_memory_image_repository.py` — implementação completa da porta, sobre listas e dicionários.
- `presentation/dependencies/__init__.py` — o composition root.
- Testes de ambos.

O `presentation/dependencies.py` (módulo solto, resíduo do scaffold) foi removido — mesmo conflito módulo-vs-pacote do RFC-011, resolvido da mesma forma.

## 4. Notas de projeto

**Providers em cadeia, não auto-construídos.** `get_index_image_use_case` declara `repository = Depends(get_image_repository)` em vez de chamar `get_image_repository()` no corpo. Isso torna **cada nível** do grafo sobrescrevível em teste: um teste pode trocar só o repositório e manter o resto real.

**O composition root importa Infrastructure de propósito.** É a única exceção às regras de fronteira — e é explícita: o teste `tests/test_ai_layer_boundaries.py` cobre `app/presentation/api/` (os routers), não a fiação.

**`InMemoryImageRepository` mora em `infrastructure/`, não em `tests/`.** Ele é um adaptador real da porta, sujeito a `mypy --strict` e ao Ruff como qualquer código de produção. E é o que permite subir a aplicação inteira sem banco — útil para desenvolvimento e para os testes de Application.

## 5. Como o composition root evoluiu

Este RFC é um bom exemplo de peça que foi projetada para mudar e mudou três vezes, sem que os consumidores precisassem mudar junto:

| RFC | Mudança |
|---|---|
| **016** | `InMemoryImageRepository` + `FakeEmbeddingModel`, singletons de módulo |
| **019** | `get_image_repository()` passa a devolver `PostgresImageRepository` |
| **023** | `get_embedding_model()` passa a devolver `ClipEmbeddingModel`, sob `@lru_cache(maxsize=1)` |
| **026** | `get_image_repository()` passa a receber a sessão via `Depends(get_db)` — corrigindo o vazamento |

Nenhum caso de uso, nenhum teste de Application e nenhuma rota precisou mudar por causa disso. É o retorno concreto de ter portas.

### 5.1 O vazamento de sessão (corrigido no RFC-026)

Vale registrar o erro, porque ele é instrutivo. A versão original era:

```python
def get_image_repository() -> ImageRepository:
    return PostgresImageRepository(SessionLocal())   # ninguém fecha isso
```

Enquanto os únicos chamadores eram testes, foi inofensivo. Quando a busca ganhou rota HTTP (RFC-026), cada requisição passou a deixar uma sessão aberta segurando uma conexão do pool **dentro de uma transação aberta**. Com pool default de 5 + 10 de overflow, requisições acima dessa taxa passam a enfileirar e expirar de forma não determinística. Pior: um backend `idle in transaction` bloqueia o autovacuum — que é o mecanismo por trás da falha que o RFC-025 §7.3 mediu por acidente, em que uma varredura HNSW devolveu 1 de 3 linhas vivas.

A correção foi uma linha:

```python
def get_image_repository(session: Session = Depends(get_db)) -> ImageRepository:
    return PostgresImageRepository(session)
```

`get_db()` (RFC-006) sempre teve o `finally: session.close()`. O bug foi contorná-lo.

### 5.2 `@lru_cache` no modelo, e a restrição que ele carrega

```python
@lru_cache(maxsize=1)
def get_embedding_model() -> EmbeddingModelPort:
    return ClipEmbeddingModel()
```

Semântica *carregar uma vez, reusar sempre*, exigida pelo RFC-023 §10 — carregar um checkpoint de 600 MB por requisição seria inviável.

**Este provider não pode ganhar parâmetros.** `IndexingWorker.main()` o importa e o chama como função de zero argumentos; acrescentar um `Depends(...)` como default entregaria ao CLI um objeto `Depends` onde ele espera um modelo, e a falha apareceria em tempo de execução, longe da edição que a causou. A restrição está documentada na própria docstring.

## 6. Testes

`tests/presentation/test_dependencies.py`

- cada provider devolve o tipo de porta declarado;
- `get_embedding_model()` devolve sempre a mesma instância (cache);
- os casos de uso vêm montados com as dependências corretas;
- `app.dependency_overrides` de fato substitui um provider (o mecanismo do qual todos os testes de rota dependem).

`tests/infrastructure/test_in_memory_image_repository.py` cobre a implementação em memória contra o contrato da porta.

## 7. Limitações

- Não há um contêiner de DI formal; usa-se o `Depends` do FastAPI. Consequência: o worker CLI, que não é FastAPI, monta seu grafo à mão em `IndexingWorker.main()`.
- Não há escopo de "requisição" fora do HTTP.
