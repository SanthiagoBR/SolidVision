# RFC-015 — Casos de Uso da Application

**Status:** Implementado
**Sprint:** 2
**Depende de:** RFC-010 (porta de repositório), RFC-013 (porta de embedding), RFC-012b
**Bloqueia:** RFC-016 (DI), RFC-021, RFC-024, RFC-025
**Commit:** `fca3f39`
**Última atualização:** 2026-08-24

---

## 1. Contexto

O Domain define *o que as coisas são*. A Infrastructure define *como falar com o mundo*. Falta a camada que define **o que o sistema faz**: em que ordem as operações acontecem, e qual regra decide entre elas.

Sem ela, essa lógica acaba dentro de um router FastAPI ou de um repositório — que é exatamente o que `AI_Context.md` proíbe (*"Never introduce business logic into Presentation or Infrastructure"*).

## 2. Decisão

Casos de uso como classes com um único método público `execute()`, recebendo suas dependências **como portas** via construtor.

```python
class IndexImageUseCase:
    def __init__(self, repository: ImageRepository, embedding_model: EmbeddingModelPort):
        self._repository = repository
        self._embedding_model = embedding_model

    def execute(self, image: Image) -> None: ...
```

Nada aqui importa SQLAlchemy, FastAPI, Torch ou `settings`.

## 3. O que foi entregue

### 3.1 `IndexImageUseCase`

```python
def execute(self, image: Image) -> None:
    if self._repository.exists(image.id):
        raise ImageAlreadyExistsError()
    self._embedding_model.encode_image(image)
    self._repository.save(image)
```

Contrato **criar-uma-vez**: rejeita duplicatas em vez de atualizá-las. A indexação incremental (RFC-021) é um caso de uso *diferente*, com política diferente — e este não foi convertido naquele, deliberadamente. Um chamador que sabe que a imagem é nova quer ser avisado se ela não for.

*(Detalhe honesto: a chamada a `encode_image()` tem seu resultado descartado, porque `ImageRepository.save()` do RFC-010 não tem como transportar um embedding. O RFC-021 resolveu isso introduzindo `IndexingRecord` e `save_indexed()`. Este caso de uso permanece como o caminho de criação simples.)*

### 3.2 `SearchImagesUseCase`

Entregue aqui em forma inicial e substancialmente ampliado pelo RFC-025. O formato atual:

```python
def execute(self, query: str, limit: int | None = None) -> list[SearchHit]:
    if not query.strip():
        raise EmptySearchQueryError(...)
    effective_limit = self._default_limit if limit is None else limit
    self._validate_limit(effective_limit)
    embedding = self._embedding_model.encode_text(query)
    return self._repository.search_similar(embedding, effective_limit)
```

O que ele **não** faz é o ponto: não detecta idioma, não traduz, não monta prompt (isso é do adaptador CLIP), não calcula distância nem ordena (isso é do repositório). Sobra o que não pertence a nenhum dos dois — decidir o que é uma requisição utilizável.

### 3.3 Limpeza

Os placeholders órfãos do RFC-001 foram removidos: `application/ports/embedding_model_port.py`, `application/use_cases/index_collection_use_case.py`, `application/use_cases/search_images_use_case.py`.

## 4. Notas de projeto

**Injeção por construtor, não construção interna.** Um caso de uso que fizesse `self._repo = PostgresImageRepository()` seria intestável sem banco, e amarraria Application a Infrastructure. Recebendo a porta, o mesmo caso de uso roda contra PostgreSQL em produção e contra `InMemoryImageRepository` no teste, sem branch nenhum.

**A camada Application não lê `settings`.** Regra descoberta na prática e depois travada por teste. `SearchImagesUseCase` recebe `default_limit` como argumento; `IndexOrUpdateImagesUseCase` (RFC-024) recebe `batch_size`. Quem transforma configuração em argumento é o *composition root* (RFC-016).

**`MAX_SEARCH_LIMIT = 100` é política de Application.** Não é limite do banco nem do repositório: existe para que uma requisição não peça à camada HTTP para serializar um resultado ilimitado. E é aplicado **levantando exceção, não truncando** — devolver 100 resultados a quem pediu 10.000 esconde a discordância dentro dos próprios dados sobre os quais o chamador vai concluir alguma coisa.

**A validação do limite roda também na construção.** Um `top_k_results` mal configurado falha ao *montar* o caso de uso, não na primeira busca que por acaso omitir `limit`.

## 5. Testes

| Arquivo | Cobre |
|---|---|
| `tests/application/fakes.py` | duplos in-process de `ImageRepository` e `EmbeddingModelPort` |
| `tests/application/test_index_image.py` | caminho feliz, rejeição de duplicata, ordem das chamadas |
| `tests/application/test_search_images.py` | query vazia/em branco, limites inválidos, default injetado, ordem preservada |
| `tests/application/test_application_architecture.py` | **nenhum módulo de `app/application/` importa `sqlalchemy`, `fastapi`, `torch` ou `settings`** |

O último é um teste de arquitetura: ele inspeciona os imports da camada. Sem ele, a regra seria apenas um parágrafo em um documento — e parágrafos não falham no CI.

## 6. Limitações

- `IndexImageUseCase` descarta o embedding que calcula (ver §3.1). Superado na prática por `IndexOrUpdateImageUseCase`.
- Não há caso de uso para deletar ou listar; o repositório expõe os métodos, mas nenhuma orquestração os usa.
- Sem fronteira transacional explícita na Application. Atomicidade aparece só no RFC-024, dentro do repositório.
