# RFC-026 — API de Busca (HTTP de ponta a ponta)

**Status:** Implementado (2026-08-23)
**Depende de:** RFC-022 (dataset de demonstração), RFC-023 (adaptador CLIP), RFC-024 (pipeline de embeddings), RFC-025 (busca semântica)
**Migration:** nenhuma — `alembic heads` continua reportando `26058b9e1d9a`, ver §5
**Medição:** `experiments/rfc-026-search-api/measure_http_latency.py`, saída em `http_latency_run_output.log`

> **Convenção de rascunho, quitada.** Todo número deste documento marcado como `TBM` era *a medir* durante a implementação e foi escrito de volta aqui depois. Do RFC-023 ao RFC-025, todos mediram primeiro e anotaram o número depois; um rascunho que fosse entregue com números chutados quebraria essa disciplina justamente no RFC onde é mais fácil quebrá-la, porque a camada HTTP não adiciona aritmética nova e convida à suposição de que os números do RFC-025 se transferem inalterados. Em boa parte, transferiram. Isso agora é uma medição, não uma dedução, e §19 registra todo ponto em que a implementação discordou deste rascunho.

---

## 1. Contexto

O RFC-025 termina com uma busca funcionando e nenhuma forma de alcançá-la:

> **§16, Não-objetivos:** *"Endpoint HTTP `/search`. Não há camada HTTP onde colocá-lo — o pacote de presentation contém placeholders de uma linha e uma rota `/health`."*
>
> **§15, Trabalho futuro, primeiro item:** *"um endpoint HTTP `/search`."*

Ou seja, o escopo deste RFC foi definido pelo anterior, deliberadamente, em vez de escolhido agora. O que existe hoje, verificado contra a árvore em vez de recordado:

| peça | estado |
| --- | --- |
| `SearchImagesUseCase` | real, 15 testes unitários, valida consulta e limite |
| `ImageRepository.search_similar()` | real, 27 testes de contrato em 3 implementações |
| `ClipEmbeddingModel` | real, preguiçoso, PT→EN, `a photo of {query}` |
| `get_search_images_use_case()` | **já ligado**, injeta `settings.top_k_results` |
| `get_embedding_model()` | **já cacheado**, `@lru_cache(maxsize=1)` |
| uma rota que chame qualquer disso | **não existe** |
| `presentation/api/v1/routers/collections.py` | uma docstring |
| `presentation/schemas/image_schema.py` | uma docstring |

O composition root é a parte que as pessoas esperam estar faltando aqui, e não está: `backend/app/presentation/dependencies/__init__.py` já compõe o caso de uso a partir de um `PostgresImageRepository` real e do adaptador CLIP real, e `tests/presentation/test_dependencies.py` já fixa essa fiação — incluindo que resolver o modelo não baixa nada.

**Este RFC é, portanto, muito menor que "construir a camada de API".** É um router, um par de schemas de resposta, um handler de exceção, um hook de inicialização, e um bug (§7) que só vira bug sob HTTP.

## 2. Problema

```
GET /api/v1/images/search?q=fish+ponds&limit=10
        ↓  FastAPI: parse, coerção, rejeição de entrada malformada
   SearchImagesUseCase.execute(query, limit)
        ↓  (RFC-023) langdetect + Marian + template + torre de texto do CLIP
        ↓  (RFC-025) pgvector <=> sobre vector(512), elegível a HNSW
   list[SearchHit]
        ↓  Presentation: mapear para DTOs
   200 {"query": ..., "limit": ..., "results": [...]}
```

A restrição que dá forma a toda decisão abaixo, e o análogo direto do *"a ordenação pertence ao banco de dados"* do RFC-025 §2: **a rota não possui nada.** Uma implementação correta que buscasse `SessionLocal`, `ClipEmbeddingModel` ou um cosseno em qualquer lugar dentro de `routers/images.py` passaria em todo teste deste RFC e teria desmontado as quatro camadas que os três RFCs anteriores gastaram todo o escopo mantendo separadas.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| Verbo e caminho | `GET /api/v1/images/search` (§4, §6) |
| Parâmetros de consulta | `q` (obrigatório), `limit` (opcional, default `settings.top_k_results`) |
| Corpo da resposta | `{query, limit, results: [{id, filename, similarity}]}` (§5) |
| `path` na resposta | **Ausente.** O sistema de arquivos do servidor não é uma interface pública (§5.2) |
| Servir bytes de imagem | **Não neste RFC.** `GET /images/{id}` é o RFC-027 (§16) |
| Indexação por HTTP | **Não neste RFC.** O CLI do worker continua sendo o único ponto de entrada (§16) |
| Erro de domínio → status | `EmptySearchQueryError`, `InvalidSearchLimitError` → **400**, via um handler de nível de aplicação (§8) |
| Parâmetro malformado/ausente → status | **422**, o próprio do FastAPI, deliberadamente não achatado para 400 (§8.1) |
| Tempo de vida da sessão | `Depends(get_db)`, uma sessão por requisição, fechada na saída (§7) |
| Estilo do endpoint | **`def`, não `async def`** — a inferência bloqueia (§9) |
| Aquecimento do modelo | Hook `lifespan`, **opt-in** via `settings.warm_up_models` (§10) |
| `/health` | Continua em `/health`, sem versão, inalterado (§6.1) |
| Migration | Nenhuma (§5) |

## 4. Arquitetura

A tabela de camadas do RFC-025 §4, estendida por uma linha. A linha nova é a única coisa que este RFC adiciona, e o que ela *não* conhece é o ponto inteiro:

| camada | conhece | não conhece |
| --- | --- | --- |
| **Presentation (nova)** | verbos HTTP, códigos de status, parsing de query string, formato JSON | que a similaridade é cosseno, que existe um vetor, que texto é traduzido, que PostgreSQL está envolvido |
| **Application** | o que torna uma requisição utilizável — consulta não vazia, `limit` na faixa | como texto vira vetor, como um vetor vira um ranking |
| **Domain** | que uma busca retorna `SearchHit`s ranqueados | SQL, pgvector, HNSW, CLIP, tradução |
| **Infrastructure (IA)** | langdetect, Marian, o template, a torre de texto | que uma busca existe |
| **Infrastructure (persistência)** | `<=>`, `vector_cosine_ops`, distância → similaridade | o que a consulta dizia, ou em que idioma |

Espera-se que o corpo da rota tenha cerca de quatro linhas. Essa é a mesma observação que o RFC-025 §4 fez sobre o caso de uso ter nove, e significa a mesma coisa: o pipeline atravessa cinco componentes especialistas, e o lugar onde ele é *exposto* não entende nenhum deles.

### 4.1 O que a rota está proibida de fazer

Declarado como lista porque também é um teste (§11.4):

- importar `sqlalchemy`, `SessionLocal` ou `EngineInstance`
- importar `torch`, `transformers`, `PIL`, `langdetect` ou `ClipEmbeddingModel`
- nomear um modelo, um template, um operador ou uma distância
- reordenar, filtrar, aplicar limiar ou truncar `SearchHit`s
- construir `SearchImagesUseCase` ela mesma em vez de recebê-lo

`tests/test_ai_layer_boundaries.py` já impõe a metade de IA disso para Domain e Application percorrendo a AST. Este RFC estende o mesmo percurso a `app/presentation/api/`, porque as regras de dependência de `AI_Context.md` listam **Presentation → Database** e **Presentation → AI Models** como proibidas e nada verifica nenhuma das duas hoje.

## 5. O contrato HTTP

### 5.1 Requisição

```
GET /api/v1/images/search?q=fish%20ponds&limit=10
```

| parâmetro | tipo | obrigatório | notas |
| --- | --- | --- | --- |
| `q` | string | sim | A consulta do usuário, em qualquer idioma que o adaptador trate |
| `limit` | inteiro | não | Omitido ⇒ `settings.top_k_results` (default 10) |

`limit` **não** recebe restrições `ge`/`le` na assinatura do FastAPI, e isso é deliberado, não um descuido — ver §8.2.

### 5.2 Resposta — 200

```json
{
  "query": "fish ponds",
  "limit": 10,
  "results": [
    {
      "id": "3f2a8c14-...",
      "filename": "fish_ponds_02",
      "similarity": 0.3255
    }
  ]
}
```

**`query` ecoa a entrada do usuário textualmente, nunca o prompt.** O RFC-025 §11.5 registrou que o Marian emite uma frase com um `". "` inicial, então a string de fato entregue ao CLIP para aquela consulta é `"a photo of . a rural property with a lake"`. Ecoar o *prompt* publicaria um artefato interno do caminho de tradução como se fosse parte do contrato, e faria a resposta mudar no dia em que o template do RFC-023 mudasse. O cliente recebe de volta o que enviou.

**`filename` é o `filename` do domínio, que não carrega extensão.** `FilesystemImageProvider` o constrói a partir de `path.stem` e mantém a extensão em um campo separado, então `fish_ponds_02.jpg` em disco é `filename="fish_ponds_02"`, `extension="jpg"` na entidade. O exemplo deste rascunho dizia `fish_ponds_02.jpg` e estava errado sobre a base de código que descrevia. A resposta publica o campo como ele é em vez de rejuntar os dois: um nome rejuntado é um valor que a Presentation inventou, e esta é a camada que deveria não inventar nada. Um cliente que precise do formato o terá pelo `Content-Type` do `GET /images/{id}` do RFC-027.

**`limit` ecoa o valor efetivo, não o requisitado.** Ele está ausente da requisição mais vezes do que não, e um chamador que recebe três resultados não tem como distinguir "só havia três imagens" de "o default é menor do que supus" sem isso. Custa um campo.

**Sem `path`.** `Image` carrega `path: ImagePath` e `PostgresImageRepository` o retorna, então isto é uma escolha de descartar informação na saída, não uma ausência dela. Duas razões, e a segunda é a estrutural:

1. Publica o layout do sistema de arquivos do servidor — `C:\Users\...` nesta máquina — para todo cliente.
2. É o identificador errado sobre o qual construir um cliente. O sistema de arquivos é a fonte de verdade (`AI_Context.md`), arquivos se movem, e o `compute_image_id` do RFC-024 deriva o id do caminho — então um cliente que armazenasse um caminho manteria algo que muda *e* algo que o `GET /images/{id}` do próximo RFC não pode aceitar.

**`similarity` é repassada sem arredondamento e sem limitação**, em `[-1, 1]`. O RFC-025 §9.1 mediu a distribuição real — mínimo **-0,1183**, melhor acerto por consulta com média de **+0,3003** — e §14 de lá rejeitou reescalar para `[0, 1]` porque isso destrói a distinção entre não relacionado (~0) e oposto (~-1). Presentation é exatamente a camada que seria tentada a "consertar" o número para uma UI, e é exatamente a camada com menos informação sobre o que o número significa. Um cliente que queira uma porcentagem pode calcular uma, tendo lido a distribuição.

**Sem migration.** Busca é uma leitura (RFC-025 §5) e HTTP não muda isso. `alembic heads` deve continuar reportando `26058b9e1d9a` como head único depois deste RFC, e §18 verifica.

### 5.3 Erros

| caso | status | corpo |
| --- | --- | --- |
| `q` presente mas em branco ou só espaços | **400** | `{"detail": "Search query cannot be empty or only whitespace."}` |
| `limit=0`, `limit=-1`, `limit=101` | **400** | `{"detail": "Search limit must be at most 100, got 101."}` |
| `q` totalmente ausente | **422** | Corpo de validação do FastAPI |
| `limit=abc` | **422** | Corpo de validação do FastAPI |

## 6. Onde a rota mora, e uma bifurcação que a árvore já contém

O repositório atualmente contém **dois** pacotes de rotas:

```
app/presentation/routes/           health.py        → registrada, sem versão
app/presentation/api/v1/routers/   collections.py   → uma docstring, não registrada
```

`AI_Context.md` documenta `presentation/api/v1/routers/` como a convenção ("Routers — um recurso por router"), então o segundo é onde o projeto já disse que rotas ficam; o primeiro é onde a única rota real de fato está. Este RFC precisa escolher um, porque adicionar a busca no que estiver mais perto é como uma base de código acaba com os dois para sempre.

**Decisão: a busca vai para `app/presentation/api/v1/routers/images.py`, montada sob `/api/v1`.** Segue a convenção documentada, e o prefixo `v1` vale a pena antes de haver clientes, e não depois — uma resposta de busca é exatamente o formato que ganha um `path`, uma URL de thumbnail ou um `total` quando uma UI existir (RFC-027), e versionar depois da publicação é uma migração, não uma decisão.

### 6.1 `/health` fica onde está

Não movida, não versionada, não tocada. Uma sonda de saúde é infraestrutura, não um recurso da API do produto: é o que um orquestrador de containers ou um balanceador de carga chama, esses são configurados com um caminho fixo, e a `v2` da API de busca não significará uma `v2` de "o banco está acessível". Deixá-la sem versão é a convenção comum e também é o diff menor — `tests/presentation/test_health.py` continua passando intocado, que é o que "não refatore módulos não relacionados" (`AI_Context.md`) pede.

O resíduo é honesto e vale registrar: depois deste RFC a árvore tem uma rota de infraestrutura sem versão e um router de recurso versionado, o que é um arranjo defensável, e `presentation/routes/` não deve adquirir uma segunda rota de recurso depois.

### 6.2 Arquivos mortos, removidos

Ambos encontrados ao ler a árvore para este rascunho, ambos sem relação com a busca exceto por estarem no pacote onde ela aterrissa:

**`app/presentation/api.py`** — um "shim de compatibilidade" cujo corpo inteiro é `from app.presentation.api import app`. Ele fica ao lado do *pacote* `app/presentation/api/`, que o sombreia: o Python resolve `app.presentation.api` para o pacote, então esse módulo nunca é importado, e se algum dia fosse, importaria a si mesmo. Não é um shim, é um arquivo que não pode rodar.

**`app/presentation/dependencies/dependencies.py`** — um placeholder de uma linha de docstring vivendo ao lado do `dependencies/__init__.py` real, ou seja, exatamente o arquivo que um leitor procurando a fiação abriria primeiro, e o único dos dois que não contém nada.

Remover ambos está em escopo porque este RFC é o primeiro trabalho em anos a tocar aquele pacote, e deixá-los significa que o próximo leitor paga o mesmo imposto. Nenhum é importado em lugar algum — §18 verifica isso antes da deleção, não depois.

Um terceiro foi junto, decidido durante a implementação: `app/presentation/schemas/image_schema.py`, que §17 deixara como "preenchido, ou removido". `search_schema.py` cobre tudo que a busca retorna, e um schema de imagem pertence ao RFC-027, que terá um payload a descrever. `collection_schema.py` é deliberadamente deixado onde está: Collections não está no caminho deste RFC, e remover um placeholder meramente por estar perto é o tipo de refatoração de passagem que `AI_Context.md` pede para evitar.

## 7. O bug de sessão que o HTTP cria

Este é o único defeito real que este RFC precisa corrigir, e ele é invisível hoje.

```python
def get_image_repository() -> ImageRepository:
    return PostgresImageRepository(SessionLocal())
```

Nada fecha aquela sessão. Nunca. Não há `try/finally`, nem gerador, nem context manager — e `get_db()`, que faz as três coisas corretamente, fica sem uso em `infrastructure/persistence/session.py` com a docstring *"Provide a database session for future FastAPI dependencies."* O futuro naquela frase é este RFC.

**Por que tem sido inofensivo.** `get_image_repository()` tem exatamente um tipo de chamador hoje: os testes, mais os dois providers irmãos que também só são chamados por testes. Verificado em vez de presumido — um grep por `get_image_repository`, `get_index_image_use_case` e `get_search_images_use_case` em `backend/` retorna `tests/presentation/test_dependencies.py` e `dependencies/__init__.py` e nada mais.

O worker de indexação notavelmente **não** o usa. `IndexingWorker.main()` abre a própria sessão e a fecha corretamente:

```python
session = SessionLocal()
try:
    worker = IndexingWorker(...)
    worker.run()
finally:
    session.close()
```

Ou seja, o tempo de vida correto já existe nesta base de código, duas vezes — em `get_db()` e no worker — e o provider é o único lugar que o ignorou.

**O que muda sob HTTP.** Toda requisição chama o provider, então toda requisição abre uma sessão, e uma sessão que executou um `SELECT` mantém uma conexão do pool dentro de uma transação aberta até que algo a feche. Nada fecha, então a conexão só é devolvida quando a `Session` é coletada pelo garbage collector e o finalizador do pool do SQLAlchemy roda. Três consequências, em ordem crescente de dificuldade de depurar:

1. **Esgotamento do pool sob concorrência.** O pool padrão do SQLAlchemy é de 5 conexões com 10 de overflow. Requisições chegando mais rápido do que o coletor de lixo recupera sessões vão enfileirar e então expirar.
2. **A falha é não determinística**, porque depende do tempo do GC. Não se reproduz em um teste manual de requisição única, não se reproduz na suíte unitária, e aparece como um timeout intermitente sob carga — a pior combinação disponível.
3. **Bloqueia o autovacuum, e o RFC-025 já mediu o que isso custa.** Uma transação de leitura não fechada é um backend `idle in transaction`, e o PostgreSQL não consegue fazer vacuum de tuplas ainda visíveis para uma delas. O RFC-025 §7.3 documenta, a partir de um acidente e não de uma teoria, que entradas mortas de índice fazem uma varredura HNSW retornar **1 de 3 linhas vivas** — os testes de contrato falharam exatamente nisso e `VACUUM ANALYZE` os corrigiu. Uma transação ociosa de vida longa é o mecanismo que mantém tuplas mortas vivas em escala. O vazamento de conexão e a degradação de recall são o mesmo bug visto por duas pontas.

**A correção.** Tornar os providers dependências do FastAPI sobre `get_db()`:

```python
def get_image_repository(session: Session = Depends(get_db)) -> ImageRepository:
    return PostgresImageRepository(session)

def get_search_images_use_case(
    repository: ImageRepository = Depends(get_image_repository),
    embedding_model: EmbeddingModelPort = Depends(get_embedding_model),
) -> SearchImagesUseCase:
    return SearchImagesUseCase(
        repository=repository,
        embedding_model=embedding_model,
        default_limit=settings.top_k_results,
    )
```

O FastAPI então fecha a sessão quando a resposta é enviada, e `app.dependency_overrides` ganha uma junta em cada nível, o que §11 usa.

`get_index_image_use_case()` teve de ir junto, o que este rascunho não dizia. Ele não tem rota HTTP e nunca terá — indexar é trabalho do worker — mas compunha a si mesmo a partir de `get_image_repository()` por chamada direta, então deixá-lo em paz teria mantido viva uma segunda cópia, não injetada, do vazamento, atrás de um provider que ainda parecia ligado.

**O custo, declarado em vez de descoberto depois.** Esses providers deixam de ser chamáveis simples. `tests/presentation/test_dependencies.py` chama `get_image_repository()` e `get_search_images_use_case()` diretamente, e com defaults `Depends(...)` essas chamadas receberiam objetos `Depends` em vez de uma sessão. Esses testes precisam ser atualizados — que é o propósito de tê-los, mas significa que o diff alcança um arquivo que parece não ter relação com "adicionar um endpoint". A alternativa — manter as funções simples e abrir uma segunda sessão, não injetada, por requisição — é rejeitada em §14.

### 7.1 `get_embedding_model()` precisa continuar diretamente chamável

Uma restrição fácil de perder e que quebraria um CLI em que ninguém estava pensando: `IndexingWorker.main()` **de fato** importa deste módulo —

```python
from app.presentation.dependencies import get_embedding_model
...
embedding_model=get_embedding_model(),
```

— e o chama como função simples de zero argumentos. Então a regra é estreita mas firme: `get_image_repository()` e `get_search_images_use_case()` podem ganhar parâmetros `Depends(...)`, porque só testes os chamam. `get_embedding_model()` **não pode**, porque um chamador não-HTTP depende de chamá-lo diretamente.

Isso não custa nada, porque um provider de zero argumentos já funciona como dependência do FastAPI — `Depends(get_embedding_model)` e `get_embedding_model()` são ambos válidos contra a mesma assinatura. É registrado porque o instinto natural ao converter o módulo é converter tudo, e o worker então falharia em tempo de execução com um objeto `Depends` cacheado por `lru_cache` fazendo as vezes de um modelo, longe da edição que causou isso. §18 roda o worker para verificar.

Vale notar de passagem, e explicitamente *não* corrigido aqui: Infrastructure importando Presentation é uma dependência invertida. A docstring de `main()` mostra que os autores estavam cientes — o import é local à função especificamente para que importar `IndexingWorker` não arraste Presentation e CLIP para um grafo de import de Infrastructure. Mover o composition root para fora de `presentation/dependencies/` é uma limpeza real e de outro RFC; este não pode mudar silenciosamente de onde o worker pega seu modelo.

## 8. Erros: um handler, e uma fronteira que vale defender

### 8.1 400 e 422 são falhas diferentes, e continuam diferentes

`EmptySearchQueryError` e `InvalidSearchLimitError` derivam ambas de `DomainError`, então um handler cobre as duas e todo erro de domínio que um caso de uso futuro adicionar:

```python
@app.exception_handler(DomainError)
def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})
```

Registrado na aplicação, não capturado na rota, para que a rota mantenha o formato que §4 exige — sem `try/except` em torno da chamada ao caso de uso.

Isso deixa uma fronteira que o contrato precisa declarar, porque é o tipo de coisa que um implementador vai "arrumar" sem perceber que é uma decisão:

| falha | quem a rejeita | status |
| --- | --- | --- |
| `?q=` — uma consulta sintaticamente correta e semanticamente vazia | `SearchImagesUseCase` | 400 |
| `?limit=101` — um inteiro fora da política da aplicação | `SearchImagesUseCase` | 400 |
| `?limit=abc` — não é inteiro | FastAPI/pydantic, antes de a rota rodar | 422 |
| nenhum `q` — a requisição não corresponde ao endpoint | FastAPI/pydantic | 422 |

A divisão é real: 422 significa *esta não é uma requisição bem formada para este endpoint*, 400 significa *esta requisição foi entendida e recusada*. Achatar 422 em 400 por arrumação apagaria isso, e colocaria a Presentation no ramo de redescrever falhas que a Application nunca viu.

### 8.2 Por que `limit` não carrega `ge`/`le` na assinatura

O FastAPI imporia `Query(ge=1, le=100)` alegremente, e é a coisa óbvia de escrever. Está rejeitado, pela razão que o RFC-025 §13.1 deu quando escolheu levantar exceção em vez de truncar: **a regra já existe, uma vez, na camada Application.**

`MAX_SEARCH_LIMIT = 100` vive em `search_images.py` com um comentário explicando que é *"política de Application em vez de um limite de repositório ou de banco."* Copiar `100` para um decorador de rota cria uma segunda cópia de uma política que não pode divergir, e a divergência é silenciosa — no dia em que alguém elevar `MAX_SEARCH_LIMIT` para 200, o endpoint continua retornando 422 em 101 e os testes do caso de uso continuam passando. O RFC-025 §5 tomou exatamente essa decisão sobre o literal `512` e nomeou a constante: *"duas cópias de um fato de esquema físico são uma cópia a mais."*

O custo é que limites fora de faixa voltam como 400 em vez de 422. Esse é o status correto de qualquer forma (§8.1): `limit=101` *é* uma requisição bem formada que a política recusa.

## 9. `def`, não `async def`

O endpoint é um `def` síncrono. Isso é uma decisão, não um default.

`encode_text()` é trabalho de CPU bloqueante — o RFC-025 §12 mediu **média 89,7 ms, mediana 87,5 ms, máx. 128,7 ms** para inglês em CPU, e ~400 ms para português incluindo tradução. `search_similar()` é uma chamada DBAPI bloqueante via psycopg. Nenhuma das duas cede o controle.

O FastAPI executa um endpoint `def` em um threadpool e um endpoint `async def` no event loop. Declarar esta rota como `async def` bloquearia o event loop por ~90–400 ms por busca, travando *toda* requisição concorrente, inclusive `/health`. A regra é excepcionalmente limpa aqui: **toda a cadeia de chamadas abaixo desta rota é síncrona, então a rota precisa ser síncrona também.**

### 9.1 O threadpool cria uma corrida que o caminho de thread única nunca teve

Rodar em um threadpool significa que requisições concorrentes compartilham um único `ClipEmbeddingModel` — `get_embedding_model()` é `@lru_cache(maxsize=1)`, o que é o design correto e permanece. Mas seu carregamento preguiçoso é um verifica-depois-age:

```python
if self._model is None or self._processor is None:
    ...
    self._model = model.to(self._device)
```

Duas requisições chegando antes de o primeiro carregamento completar podem ambas avaliar `self._model is None` como verdadeiro e ambas carregar o checkpoint — cerca de 5 s e várias centenas de MB cada, em um processo frio, concorrentemente. `lru_cache` não impede isso: ele faz com que compartilhem o *adaptador*, e a corrida está dentro do adaptador.

Duas correções candidatas, e este RFC prefere a primeira:

1. **Aquecer na inicialização (§10).** Se o modelo estiver carregado antes de o servidor aceitar sua primeira requisição, nenhuma requisição jamais observa `_model is None`, e a corrida não tem janela. Isso não custa nada em tempo de requisição.
2. Um lock em `_ensure_loaded()`. Correto, mas é uma mudança no componente do RFC-023 para resolver um problema introduzido pelo threadpool do RFC-026, e coloca um lock no caminho quente de toda codificação pelo resto da vida do processo.

**A ressalva honesta:** a opção 1 fecha a janela apenas quando o aquecimento está habilitado, e §10 torna o aquecimento opt-in. Com `WARM_UP_MODELS=false` — o padrão — a corrida é real, mas estreita: exige duas requisições dentro dos primeiros ~5 s de um processo frio, e seu pior desfecho é trabalho e memória duplicados, não uma resposta errada. Se isso for julgado inaceitável, o lock é a resposta e pertence a uma continuação do RFC-023, não contrabandeado aqui. Isso está escrito para que a escolha seja visível em vez de descoberta.

## 10. Aquecimento, e a armadilha na implementação óbvia

O RFC-025 §12 atribuiu este trabalho a este RFC pelo nome:

> *"A primeira consulta de um processo paga ~4,95 s para carregar o checkpoint do CLIP, e a primeira consulta em português paga mais ~4,20 s pelo tradutor Marian. Ambas estouram a meta de 1 segundo uma vez por processo. […] a correção — aquecer o modelo na inicialização — pertence ao RFC que introduzir a camada HTTP, onde 'inicialização' passa a significar alguma coisa."*

Então: um handler `lifespan` que resolve `get_embedding_model()` e codifica uma string descartável, antes de o servidor ficar pronto.

**A string descartável é em português, e esse é o ponto inteiro.** O RFC-025 mediu *duas* partidas a frio e este rascunho citou as duas, e então especificou um aquecimento que fecha uma. `encode_text()` só carrega o Marian quando o detector chama a entrada de português, então um aquecimento em inglês deixa a carga de ~4,20 s do tradutor no lugar para o primeiro usuário brasileiro de cada processo — em um produto cujos usuários escrevem português. Uma frase em português percorre detectar → traduzir → aplicar template → codificar e carrega os dois modelos. É uma *frase* pela razão que o RFC-023 §7.1 mediu: o `langdetect` precisa de várias palavras, então uma string curta seria lida como outro idioma, pularia a tradução, e aqueceria apenas o CLIP enquanto se parecesse exatamente com um aquecimento que funcionou. `test_the_warm_up_query_actually_reaches_the_translator` fixa isso.

Usar `lifespan`, não `@app.on_event("startup")` — o último está depreciado no FastAPI atual, e este é o primeiro hook de inicialização do projeto, então não há estilo existente a seguir nem razão para adotar o depreciado.

### 10.1 A armadilha

**Um aquecimento que roda incondicionalmente quebra a suíte de testes offline.**

`tests/presentation/test_health.py` abre a aplicação como `with TestClient(app) as test_client:`, e a forma `with` é precisamente o que dispara eventos de inicialização. Um aquecimento incondicional, portanto, baixaria e carregaria o CLIP durante um `pytest` puro — violando a regra do RFC-023 §15 de que a suíte rápida nunca alcança o Hugging Face, em um arquivo de teste que não tem nada a ver com busca, por uma rota que não usa o modelo.

Isso merece o espaço porque é uma armadilha silenciosa: o código de aquecimento pareceria certo, os testes de busca passariam, e o dano apareceria como `test_health.py` de repente levando 5 s e precisando de rede.

### 10.2 A decisão: opt-in, com padrão desligado

```python
warm_up_models: bool = Field(
    default=False,
    description="Load the embedding model at API startup instead of on first request",
)
```

Padrão `False`, com `WARM_UP_MODELS=true` em `.env.example` para quem rodar a API de verdade.

O padrão é escolhido do mesmo modo que o RFC-024 §12 escolheu tornar `--root` obrigatório sem default — *"para que o comando nunca possa começar a indexar uma coleção real de fotos que ninguém apontou."* Mesmo princípio: **o padrão seguro é aquele que não pode baixar 600 MB para dentro de um processo que nunca pediu.** Uma execução de teste, um job de CI, um `import app.presentation.api` no REPL de um desenvolvedor — nenhum deles quer um checkpoint, e todos eles receberiam um sob `default=True`.

O custo é real e declarado com clareza: **de fábrica, a API não atende à meta de latência na primeira consulta.** Ela paga a partida a frio de ~5 s do RFC-025, exatamente como documentado lá. Ligar o aquecimento é uma linha no `.env`, e §18 mede os dois caminhos em vez de afirmar que a configuração funciona.

**Um aquecimento que falha é logado, não fatal.** O rascunho não dizia, e a escolha não é óbvia. Abortar a inicialização derrubaria `/health` junto com a busca — a sonda que um operador usaria para diagnosticar exatamente isso — e transformaria uma indisponibilidade transitória do Hugging Face em um loop de crash. Então a exceção é logada em `WARNING` com seu traceback e o servidor inicia mesmo assim; o caminho preguiçoso está intocado, então a primeira consulta tenta a carga de novo e falha visivelmente se ainda não conseguir. O custo é que uma má configuração aparece como uma linha de aviso em vez de uma recusa a iniciar, que é para o que a linha serve. `test_a_failed_warm_up_does_not_stop_the_server` fixa isso.

**Medido** (esta máquina, CPU, cache de disco do Hugging Face aquecido):

| | inicialização |
| --- | --- |
| `WARM_UP_MODELS=false` | **0,00 s** |
| `WARM_UP_MODELS=true` | **8,37 s** |

Com o aquecimento ligado, a meta de 5 s de inicialização de `ARCHITECTURE.md` §22 é perdida por 3,4 s, e este rascunho previu que seria. O número não é surpresa nem defeito: são as duas partidas a frio do RFC-025 (~4,95 s CLIP, ~4,20 s Marian) movidas das duas primeiras consultas para a inicialização, onde são pagas uma vez por ninguém que esteja esperando uma resposta. A reconciliação que §10 prometeu é que a meta de 5 s foi escrita para um processo que não carrega modelos, e um processo que carrega dois não consegue atendê-la — enquanto o que um usuário de fato experimenta, a primeira busca, cai de ~5 s para ~0,13 s. Ou a meta limita um processo que aquece preguiçosamente, caso em que o aquecimento está fora do escopo dela, ou pretende limitar o boot inteiro, caso em que precisa de um número dentro do qual um checkpoint caiba. Essa é uma decisão de quem for dono de §22, registrada aqui em vez de silenciosamente omitida.

## 11. Testes

Três níveis, e a razão de haver três é que cada um pode falhar enquanto os outros passam.

### 11.1 Nível 1 — testes unitários de rota (rápidos, sempre executados)

`TestClient` + `app.dependency_overrides[get_search_images_use_case]` retornando um stub. Sem banco, sem modelo, sem rede.

| teste | fixa |
| --- | --- |
| 200 com o corpo documentado para uma consulta normal | §5.2 |
| `query` ecoa a entrada crua, não um prompt | §5.2 |
| `limit` omitido ⇒ a resposta ecoa `settings.top_k_results` | §5.2 |
| a rota repassa `q` e `limit` **sem modificação** | §4 |
| os resultados aparecem na ordem do repositório, não reordenados pela rota | RFC-025 §4 |
| `similarity` sobrevive como float, valores negativos incluídos | §5.2 |
| `path` está **ausente** de todo objeto de resultado | §5.2 |
| `?q=` ⇒ 400 com a mensagem de domínio | §8.1 |
| `?limit=0` / `?limit=101` ⇒ 400 | §8.1 |
| `q` ausente ⇒ 422 | §8.1 |
| `?limit=abc` ⇒ 422 | §8.1 |
| conjunto de resultados vazio ⇒ 200 com `"results": []`, não 404 | — |

O último merece sua linha: "nenhuma imagem correspondeu" é uma busca bem-sucedida sem nada dentro, não um recurso ausente. 404 significaria que o *endpoint* não foi encontrado.

### 11.2 Nível 2 — integração de API (Postgres real, modelo fake)

`TestClient` + o grafo de dependências real + `empty_db_session` + `FakeEmbeddingModel` substituindo o adaptador CLIP.

Este é o nível que prova a fiação, e a razão de existir separado do nível 3 é que ele exercita **injeção de dependência, tempo de vida da sessão, o repositório, o pgvector e a serialização JSON** sem pagar ~5 s por um checkpoint. `FakeEmbeddingModel` serve exatamente para isso: é determinístico entre processos (SHA-256 em modo contador, não `hash()`), produz vetores unitários centrados na média em `settings.embedding_dimension`, e sua própria docstring registra que sua geometria foi corrigida no RFC-022 §7.4 para que a busca por similaridade sobre ela não seja trivialmente degenerada. Ele não tem semântica — o que está tudo bem, porque semântica é trabalho do nível 3.

| teste | fixa |
| --- | --- |
| uma requisição sobre linhas semeadas as retorna ranqueadas, a melhor primeiro | a cadeia inteira |
| a linha com embedding NULL nunca aparece | RFC-025 §6 |
| `limit` trunca o conjunto de resultados real | §5.1 |
| os ids na resposta são os ids no banco | §5.2 |
| **a sessão é fechada quando a resposta é enviada** | §7 |

Esse último teste é a razão de este nível existir, e o mecanismo é o que o rascunho supôs: `QueuePool.checkedout()` antes e depois de cinco requisições sequenciais, com `get_db` deliberadamente *não* sobrescrito, de modo que o `finally: session.close()` do provider real é o que está sendo medido. A guarda `isinstance(pool, QueuePool)` é estrutural — sob `NullPool` toda leitura seria zero e o teste passaria por construção.

**Verificado contra o bug, não contra a correção.** Restaurar o antigo corpo de uma linha `PostgresImageRepository(SessionLocal())` foi tentado, e ele falha **quatro dos cinco** testes deste nível enquanto deixa os outros 34 testes rápidos de presentation verdes. Os quatro falham por duas razões diferentes, e o par vale separar:

* o teste de pool falha pelo vazamento em si — conexões nunca voltam;
* outros três falham porque o provider antigo ignora sua sessão injetada inteiramente, então uma requisição deixa de ler a transação que a fixture semeou e ranqueia o que o banco de desenvolvimento por acaso contiver.

A segunda razão é a que explica por que o bug ficou invisível por tanto tempo: nada antes deste RFC jamais pediu que uma *requisição* enxergasse uma linha que um *teste* havia escrito, porque nada antes deste RFC emitia requisição. Os níveis 1 e 3 permanecem verdes o tempo todo, exatamente como deveriam — o nível 1 usa stub no caso de uso e o nível 3 sobrescreve o repositório, então nenhum dos dois tem opinião sobre de onde vem uma sessão. Essa é a verificação que o rascunho pediu em §7: um teste que meramente afirma um 200 passa direto pelo vazamento.

### 11.3 Nível 3 — verdadeiro fim a fim (`slow`)

CLIP real, PostgreSQL real, pgvector real, sobre HTTP. Marcado como `slow` e desselecionado pelo `addopts` padrão, seguindo o RFC-023 §12.1.

**Deliberadamente pequeno.** `tests/dataset/test_semantic_search_e2e.py` já roda todas as 25 consultas de *ground truth* pela pilha real, com pisos de regressão medidos (Recall@5 ≥ 76%, contaminação ≤ 20%, top-1 estrito ≥ 56%, pairwise ≥ 72%). Reexecutar métricas de qualidade por HTTP mediria o modelo uma segunda vez e chamaria isso de teste de API. A pergunta que este nível responde é mais estreita:

> Uma requisição HTTP real alcança o CLIP real e o pgvector real e volta como JSON correto?

| teste | asserção |
| --- | --- |
| uma consulta em inglês retorna uma imagem relevante no top 5 | reutiliza o *ground truth* existente |
| uma consulta em português em frase completa retorna resultados sobrepostos | prova que a tradução está no caminho *HTTP* |

O teste em português é em frase completa pela razão que o RFC-023 §7.1 mediu: o `langdetect` chama `fazenda` de turco e `lago` de tagalo, então uma consulta de uma palavra chega ao CLIP sem tradução e testaria o caminho PT-bruto enquanto alegasse testar a tradução.

**Nenhum corpus de fixture novo.** O corpus de demonstração do RFC-022, seu manifesto, seu `queries.json` e a fixture de indexação com escopo de módulo já existem e já estão corretamente isolados. Um segundo dataset de quatro imagens seria um *ground truth* paralelo a manter, divergindo do primeiro, para responder a uma pergunta que o primeiro consegue responder.

### 11.4 O teste de fronteira

Estender o percurso de AST de `tests/test_ai_layer_boundaries.py` a `app/presentation/api/`, garantindo nenhum import de `sqlalchemy`, `psycopg`, `pgvector`, `torch`, `transformers`, `PIL`, `langdetect`, ou qualquer módulo cujo nome contenha `clip`.

`AI_Context.md` lista **Presentation → Database** e **Presentation → AI Models** como proibidas, e nada verifica nenhuma das duas hoje — a regra vinha sendo trivialmente satisfeita apenas porque a Presentation estava vazia. Este RFC é o primeiro commit que poderia violá-la, então é o commit que deve torná-la verificável. Note a isenção deliberada: `dependencies/` **precisa** importar as duas, porque compor Infrastructure é para o que serve um composition root. O percurso cobre os routers, não a fiação.

## 12. Desempenho

Medido por `experiments/rfc-026-search-api/measure_http_latency.py`: o corpus de 45 imagens do RFC-022 indexado dentro de uma transação desfeita, um servidor uvicorn real em um socket real, e toda consulta cronometrada duas vezes — uma através de `SearchImagesUseCase.execute()` e uma através de HTTP — para que a diferença seja a resposta. `TestClient` deliberadamente não foi usado para cronometrar: ele conduz a aplicação ASGI em processo e teria medido a camada com seu transporte removido, que é justamente a parte em questão. 20 repetições por linha, com o processo aquecido.

| | média | mediana | p95 | máx. |
| --- | --- | --- | --- | --- |
| Inglês, caso de uso direto | 130,8 ms | 124,2 ms | 168,2 ms | 176,3 ms |
| **Inglês, sobre HTTP** | **131,1 ms** | 127,8 ms | 152,3 ms | 155,6 ms |
| Português, caso de uso direto | 393,1 ms | 375,1 ms | 527,3 ms | 544,3 ms |
| **Português, sobre HTTP** | **381,5 ms** | 371,0 ms | 430,2 ms | 472,1 ms |

**A camada HTTP custa +0,3 ms em inglês e −11,6 ms em português.** O número negativo é o útil: não é um ganho de velocidade, é prova de que o delta é menor que o ruído entre execuções daquilo que está sendo medido. Roteamento, resolução de dependências, ordenação de `vector(512)` e serialização JSON juntos se perdem dentro da variância de um único forward pass do CLIP. Nada aqui vale otimizar, e a razão é que nunca houve aritmética nesta camada a otimizar.

Contra as metas:

| meta (`ARCHITECTURE.md` §22) | RFC-025 medido | RFC-026 sobre HTTP |
| --- | --- | --- |
| Latência de busca < 1 s | ~90 ms inglês aquecido (caso de uso) | **131 ms**, atendida |
| | ~400 ms português aquecido (caso de uso) | **382 ms**, atendida |
| | 0,36 ms de banco com 45 linhas | inalterado, sem mudança na consulta |
| Inicialização < 5 s | modelos carregam preguiçosamente | **0,00 s** desligado / **8,37 s** ligado (§10.2) |
| Primeira requisição a frio | ~4,95 s CLIP + ~4,20 s Marian | removida pelo aquecimento; ~5 s sem ele |

Duas ressalvas honestas sobre os números absolutos, nenhuma das quais toca o delta. Inglês deu 131 ms aqui contra os 89,7 ms do RFC-025 na mesma máquina e no mesmo checkpoint; a execução não foi feita em um laptop ocioso, e este experimento nunca alegou remedir a torre de texto. E a dispersão do p95 em português (430–544 ms) é o comprimento de geração do Marian variando, não a API.

**Nenhuma meta nova de latência é definida.** Os números de §12 do RFC-025 foram medidos nesta máquina, em CPU, e uma meta inventada aqui sem medir o deploy real seria exatamente aquilo contra o que a revisão deste plano advertiu.

## 13. Configuração

| configuração | mudança |
| --- | --- |
| `top_k_results` | inalterada. Já é o default injetado (RFC-025 §13.1); o endpoint agora a expõe como o `limit` efetivo |
| `warm_up_models` | **nova**, `bool`, default `False` (§10.2) |
| `MAX_SEARCH_LIMIT` | inalterada, permanece em `search_images.py`, não duplicada na rota (§8.2) |

Nenhuma configuração nova de IA, banco ou ordenação. Qualquer pressão para adicionar `SEARCH_DEFAULT_LIMIT`, `SEARCH_MAX_LIMIT` ou um limiar de similaridade do lado da API é sinal de que a rota está adquirindo política, e pertence a §16.

## 14. Alternativas consideradas

| alternativa | por que não |
| --- | --- |
| `POST /images/search` com corpo JSON | Busca é uma leitura: cacheável, linkável, idempotente, expressável como uma URL que um usuário pode salvar e um log pode registrar. Um corpo não compra nada para uma string e um inteiro |
| Retornar `path` na resposta | Publica o sistema de arquivos do servidor e entrega aos clientes um identificador que muda quando um arquivo se move (§5.2) |
| Servir bytes de imagem neste RFC | Precisa de defesa contra path traversal, negociação de content-type, requisições de range, cache e uma decisão sobre thumbnails. Isso é o RFC-027, e agrupá-lo faria do RFC de E2E o maior até agora |
| `POST /index` ao lado da busca | Indexar é trabalho do worker (`AI_Context.md`: *"a indexação é executada pelo Worker, nunca pelo FastAPI"*). Sobre HTTP, adicionalmente precisa de autenticação, uploads, estado de job, progresso, cancelamento e validação de caminho |
| `Query(ge=1, le=100)` em `limit` | Duplica `MAX_SEARCH_LIMIT` em uma segunda camada onde ele vai divergir silenciosamente (§8.2) |
| Endpoint `async def` | Bloqueia o event loop por 90–400 ms por busca; toda a cadeia abaixo é síncrona (§9) |
| Manter `get_image_repository()` como função simples e abrir uma sessão por requisição dentro da rota | A rota então importaria `SessionLocal`, violando §4.1, e ainda teria de fechá-la à mão |
| Capturar erros de domínio com `try/except` na rota | Coloca política de erro em toda rota em vez de uma vez na aplicação, e cria um desvio por tipo de exceção (§8.1) |
| Achatar 422 em 400 | Apaga a diferença entre uma requisição malformada e uma recusada (§8.1) |
| Arredondar ou reescalar `similarity` para exibição | Presentation tem a menor informação sobre o que a pontuação significa, e o RFC-025 §14 já rejeitou o reescalonamento |
| Aquecer incondicionalmente na inicialização | Baixa o CLIP durante um `pytest` puro, via `test_health.py` (§10.1) |
| Um corpus de fixture E2E dedicado de quatro imagens | Um segundo *ground truth* a manter, respondendo a uma pergunta que o corpus do RFC-022 já responde (§11.3) |
| Remedir Recall@5 por HTTP | Mede o modelo duas vezes e chama a segunda de teste de API (§11.3) |
| Um frontend neste RFC | Não é preocupação de backend, e esconderia se a API é utilizável por qualquer outra coisa. RFC-028 |

## 15. Riscos e trabalho futuro

| risco | situação |
| --- | --- |
| **A partida a frio ainda estoura a meta de latência** quando o aquecimento está desligado, que é o padrão (§10.2) | Aceito e documentado; uma linha no `.env` o liga |
| **O aquecimento ligado perde a meta de 5 s de inicialização**, medido em 8,37 s em processo e 11,23 s em um servidor novo (§10.2, §18.1) | Aceito. São as duas cargas de modelo do RFC-025 movidas das primeiras consultas para o boot. `ARCHITECTURE.md` §22 precisa dizer se seus 5 s limitam um processo que carrega modelos |
| **A corrida de carregamento preguiçoso** é fechada pelo aquecimento, mas fica aberta sem ele (§9.1) | Janela estreita, sem respostas erradas; o lock pertence a uma continuação do RFC-023 |
| **Sem autenticação.** O endpoint está aberto a qualquer um que alcance a porta | Não-objetivo explícito (§16). Produto local-first, nenhum modelo multiusuário existe ainda |
| **Sem limitação de taxa**, e cada requisição custa ~90–400 ms de inferência em CPU | Superfície real de DoS no momento em que isto for exposto além do localhost. Pertence junto com a autenticação |
| **Sem paginação além de `limit`.** Sem offset, sem cursor, sem total | Deliberado. Um `total` sobre uma consulta top-K por similaridade é outra consulta, e paginação por offset sobre um índice ANN não é bem definida |
| **O recall do HNSW continua não validado** (RFC-025 §7.1) | Inalterado por este RFC; ainda precisa do benchmark de 100k |
| **O isolamento de testes ainda trava a tabela** e assume suíte de processo único (RFC-025 §10.1) | Inalterado; revisitar antes de `pytest-xdist` |
| **Dois pacotes de rota** agora coexistem por decisão, e não por acidente (§6.1) | Documentado; `presentation/routes/` não recebe mais rotas de recurso |

Trabalho futuro, em ordem aproximada de valor: `GET /images/{id}` e thumbnails (RFC-027); uma UI de busca (RFC-028); o benchmark de 100k que de fato exercitaria o HNSW e o `ef_search`; autenticação e limitação de taxa; um filtro de similaridade mínima escolhido a partir da distribuição do RFC-025 §9.1; busca escopada por coleção quando `Collections` existir.

## 16. Não-objetivos

Confirmados fora de escopo, e cada um é uma decisão em vez de um descuido:

**Não tocar no que RFCs anteriores decidiram**

- o modelo de embedding, checkpoint, dimensão, template ou tradução (RFC-023)
- o pipeline de indexação, batching, hashing ou escritas em lote (RFC-024)
- a ordenação: o operador, o desempate, a faixa de pontuação, ajuste de HNSW (RFC-025)
- qualquer migration, coluna ou índice novo (§5)
- remedir a qualidade da recuperação (§11.3)

**Ainda não construir**

- servir bytes de imagem, `GET /images/{id}`, thumbnails, CDN, mounts estáticos
- qualquer frontend, UI ou galeria
- indexação por HTTP, uploads, filas de job, progresso, cancelamento
- autenticação, autorização, multiusuário, chaves de API, limitação de taxa
- paginação além de `limit`: offset, cursores, `total`, `has_more`
- busca imagem-para-imagem, busca híbrida, full-text, reranking, cross-encoders
- filtros de metadados: data, localização, extensão, coleção
- `SearchHistory`, buscas salvas, autocompletar, sugestões de consulta
- cache de embeddings de consulta ou de conjuntos de resultados
- WebSockets, streaming, busca assíncrona, endpoints de consulta em lote
- OpenTelemetry, endpoints de métricas, tracing distribuído
- containerizar a API, deploy, proxy reverso, TLS

### 16.1 Observabilidade: o mínimo, e seu limite

Duas linhas de log, em `INFO`, através do `get_logger` existente:

```
search requested: query_length=11, limit=10
search completed: results=5, elapsed_ms=94
```

**O texto da consulta em si não é logado, e o embedding nunca é.** O comprimento da consulta é suficiente para correlacionar uma requisição lenta com uma consulta longa; o texto é o histórico de busca de um usuário escrito em um arquivo que sobrevive à requisição, e este é um produto local-first cuja premissa inteira é que os dados do usuário continuam dele. Logá-lo é uma decisão de produto e de privacidade, não uma conveniência de depuração — se for desejado, deve ser uma configuração com padrão desligado, defendida em seu próprio RFC.

## 17. Entregáveis

Como entregue. Três arquivos diferem do que o rascunho listava, e cada um está marcado.

**Novos**

| arquivo | propósito |
| --- | --- |
| `backend/app/presentation/api/v1/routers/images.py` | A rota de busca (§6) |
| `backend/app/presentation/schemas/search_schema.py` | `SearchResultSchema`, `SearchResponseSchema` (§5.2) |
| `backend/app/presentation/error_handlers.py` | `DomainError` → 400 (§8) |
| `backend/tests/presentation/test_search_route.py` | Nível 1 (§11.1) |
| `backend/tests/presentation/test_search_api_integration.py` | Nível 2 (§11.2) |
| `backend/tests/presentation/test_search_api_e2e.py` | Nível 3, `slow` (§11.3) |
| `backend/tests/presentation/test_api_startup.py` | **Não estava no rascunho.** O aquecimento é uma decisão com três ramos — desligado, ligado, falho — e §10.1 o chama de armadilha silenciosa. Uma armadilha que nada testa é uma armadilha |
| `experiments/rfc-026-search-api/measure_http_latency.py` | A medição por trás de §12 |
| `experiments/rfc-026-search-api/http_latency_run_output.log` | Sua saída, mantida do jeito que o RFC-023 e o RFC-025 mantiveram as suas |
| `docs/rfcs/rfc-026-api-de-busca.md` | Este documento |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/presentation/api/__init__.py` | Monta o router v1; registra o handler; adiciona `lifespan` (§10) |
| `backend/app/presentation/api/v1/__init__.py` | **Não estava no rascunho.** A docstring placeholder vira o router de prefixo `/api/v1` |
| `backend/app/presentation/api/v1/routers/__init__.py` | **Não estava no rascunho.** Exporta `images_router`, no mesmo estilo de `routes/__init__.py` |
| `backend/app/presentation/dependencies/__init__.py` | Os três providers de caso de uso/repositório passam a usar `Depends` sobre `get_db` (§7) |
| `backend/app/infrastructure/config/settings.py` | `warm_up_models` (§10.2) |
| `backend/tests/presentation/test_dependencies.py` | Atualizado para as novas assinaturas dos providers (§7) |
| `backend/tests/test_ai_layer_boundaries.py` | Percorre `app/presentation/api/` (§11.4) |
| `.env.example` | `WARM_UP_MODELS` |

**Deletados**

| arquivo | razão |
| --- | --- |
| `backend/app/presentation/api.py` | Sombreado pelo pacote `api/`; importaria a si mesmo se fosse alcançável (§6.2) |
| `backend/app/presentation/dependencies/dependencies.py` | Placeholder vazio ao lado da fiação real (§6.2) |
| `backend/app/presentation/schemas/image_schema.py` | **Deletado, não preenchido.** O rascunho deixou a escolha em aberto; `search_schema.py` cobre tudo que a busca retorna, e um schema de imagem é do RFC-027, quando ele tiver um payload a descrever. `collection_schema.py` fica em paz — Collections não está no caminho deste RFC |

## 18. Validação

Preenchida a partir de uma execução real de 2026-08-23. Toda linha é um comando, não uma alegação.

| verificação | esperado | resultado |
| --- | --- | --- |
| `pytest` | 471 + os novos testes rápidos, 0 falhas | **502 passaram**, 58 desselecionados, 46,9 s. +31: 14 de nível 1, 5 de nível 2, 4 de inicialização, 3 de dependências, 5 de fronteira |
| `pytest` não alcança a rede | `test_health.py` ainda roda offline e rápido (§10.1) | **38 passaram em 18,6 s** com `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` |
| `pytest -m slow` | 56 + os 2 novos testes E2E | **58 passaram**, 502 desselecionados, 168,6 s |
| `black --check .` | limpo | **131 arquivos inalterados** |
| `ruff check .` | limpo | **All checks passed** |
| `mypy` | 5 erros preexistentes, **0 em código novo ou modificado** | **5 erros em 4 arquivos**, todos preexistentes (2 testes de instanciação abstrata, 1 inalcançável, 1 de tipo de argumento, 1 de retorno de gerador) |
| `alembic heads` | `26058b9e1d9a (head)`, inalterado | **`26058b9e1d9a (head)`** |
| Imports do router de Presentation | sem `sqlalchemy`, `torch`, `transformers`, `PIL`, `langdetect`, `clip` (§11.4) | Imposto por `test_ai_layer_boundaries.py`, agora percorrendo três diretórios |
| Imports da camada Application | inalterados desde o RFC-025 | Mesmo percurso, ainda verde |
| Conexões devolvidas após N requisições | contagem de checkout do pool de volta à linha de base (§11.2) | `QueuePool.checkedout()` idêntico após 5 requisições. Reintroduzir o provider antigo falha **4 dos 5** testes de nível 2 e nenhum outro (§11.2) |
| Os três arquivos deletados | `grep` mostra nenhum importador antes da deleção (§6.2) | Verificado antes de deletar. `from app.presentation.api import app` em `main.py` e `test_health.py` resolve para o *pacote*, que é o que sombreava o shim |
| Worker de indexação | O CLI ainda roda; `get_embedding_model()` ainda é de zero argumentos (§7.1) | `python -m app.infrastructure.workers.indexing_worker --root <tmp>` → **Discovered 1, Indexed 1, Failed 0** com o checkpoint real. A linha que ele escreveu foi apagada depois |
| Banco de desenvolvimento | inalterado; todo teste desfeito | **3 linhas antes, 3 linhas depois**, mesmos ids |
| `GET /api/v1/images/search?q=fish+ponds` à mão | 200, JSON ranqueado, contra o corpus real indexado | **200 em 0,130 s**, `fish_ponds_02` primeiro com `+0,3255`, `cleared_lot_04` por último com `+0,2256` |

### 18.1 O endpoint à mão

Contra um `uvicorn main:app` real com `WARM_UP_MODELS=true`, sobre o banco de desenvolvimento. A inicialização logou `Embedding model warmed up in 11.23s` — maior que os 8,37 s de §10.2, e o mais honesto dos dois números: o script de medição já havia tocado os dois checkpoints no mesmo processo, então seu cache de disco estava quente de um jeito que o de um servidor recém-iniciado não está.

```
GET /health
{"status":"healthy","database":"connected","version":"0.1.0","environment":"development"}

GET /api/v1/images/search?q=fish+ponds                          200, 0.130 s
{"query":"fish ponds","limit":10,"results":[
  {"id":"8438ee26-...","filename":"fish_ponds_02","similarity":0.3255467622820445},
  {"id":"b082aa81-...","filename":"fish_ponds_01","similarity":0.3198034978147175},
  {"id":"603c940f-...","filename":"cleared_lot_01","similarity":0.2645992917300134},
  {"id":"80e09c3a-...","filename":"cleared_lot_04","similarity":0.22564180340518103}]}

GET /api/v1/images/search?q=uma%20propriedade%20rural%20com%20um%20lago&limit=3
                                                                200, 0.682 s
{"query":"uma propriedade rural com um lago","limit":3,"results":[
  {"id":"b082aa81-...","filename":"fish_ponds_01","similarity":0.2724458141592635},
  {"id":"8438ee26-...","filename":"fish_ponds_02","similarity":0.27095358198604524},
  {"id":"603c940f-...","filename":"cleared_lot_01","similarity":0.267724827988436}]}

GET /api/v1/images/search?q=                                    400
{"detail":"Search query cannot be empty or only whitespace."}

GET /api/v1/images/search?q=x&limit=101                         400
{"detail":"Search limit must be at most 100, got 101."}

GET /api/v1/images/search                                       422
{"detail":[{"type":"missing","loc":["query","q"],"msg":"Field required",...}]}
```

Quatro coisas a extrair disso, nenhuma das quais um teste afirma com a mesma clareza:

**O eco da consulta é a entrada crua**, português e tudo, enquanto o ranking que ela produziu é `fish_ponds_01`, `fish_ponds_02`, `cleared_lot_01` — a vizinhança traduzida. O prompt que o CLIP viu não está em lugar nenhum da resposta (§5.2).

**Nenhum `path` em lugar algum**, em um banco cujas linhas contêm `C:/Users/chapi/Documents/...` (§5.2).

**`limit` ecoa 10 quando nunca foi enviado**, e 3 quando foi (§5.2).

**400 e 422 chegam de lugares diferentes e dizem coisas diferentes**, que é a fronteira que §8.1 existe para manter (§8.1).

O contrato de observabilidade também se sustenta — o log da execução acima diz:

```
search requested: query_length=10, limit=10
search completed: results=4, elapsed_ms=119
search requested: query_length=0, limit=10
search requested: query_length=33, limit=3
search completed: results=3, elapsed_ms=674
```

Nenhum texto de consulta, nenhum embedding, nunca (§16.1). Note que a requisição recusada loga um `requested` sem `completed`, que é exatamente o formato que um 400 deve deixar para trás.

Uma ressalva declarada em vez de enterrada: o corpus que essas requisições ranquearam são as quatro linhas que o banco de desenvolvimento por acaso continha — três deixadas por um teste do RFC-024 mais a que a verificação do worker indexou, desde então apagada. A *qualidade* de recuperação é medida sobre o corpus de 45 imagens por `tests/dataset/`, não aqui; esta seção é evidência de que o transporte funciona, não de que o modelo funciona.

## 19. O que a implementação mudou neste rascunho

Mantido como seção própria em vez de diluído no texto acima, porque um plano que se lê como se tivesse previsto tudo é um plano com o qual ninguém aprende. Seis coisas se moveram, e nenhuma delas mudou uma decisão de §3 — que é o resultado útil: o formato estava certo, e o que o rascunho errou foi a árvore que descrevia e as perguntas que não pensou em fazer.

| # | o rascunho dizia | o que foi entregue | por quê |
| --- | --- | --- | --- |
| 1 | `"filename": "fish_ponds_02.jpg"` | `"fish_ponds_02"` | `Image.filename` é `path.stem`; a extensão é um campo separado. O rascunho descrevia uma base de código que não existe. A resposta publica o campo em vez de rejuntar os dois, porque um nome rejuntado é um valor que a Presentation inventou (§5.2) |
| 2 | o aquecimento "codifica uma string descartável" | a string é uma frase em português | Um aquecimento em inglês carrega o CLIP e deixa os ~4,20 s do Marian no lugar para a primeira consulta em português — metade da partida a frio que o rascunho citou, em um produto cujos usuários escrevem português (§10) |
| 3 | dois providers ganham `Depends` | os três ganharam | `get_index_image_use_case()` compunha a si mesmo a partir de `get_image_repository()` por chamada direta, então deixá-lo teria mantido viva uma segunda cópia do vazamento de sessão (§7) |
| 4 | nada sobre falha de aquecimento | logado em `WARNING`, a inicialização continua | Abortar derrubaria `/health` junto com a busca e transformaria uma oscilação do Hugging Face em um loop de crash (§10.2) |
| 5 | nenhum arquivo de teste de inicialização | `test_api_startup.py` | §10.1 chama o aquecimento incondicional de armadilha silenciosa. Uma armadilha que nada testa é uma armadilha (§17) |
| 6 | `image_schema.py` "preenchido, ou removido" | removido | `search_schema.py` cobre tudo que a busca retorna; um schema de imagem é do RFC-027, quando ele tiver um payload (§17) |

E uma previsão que se sustentou exatamente, digna de registro porque era o risco em torno do qual este RFC foi escrito: **a rota acabou com quatro linhas de trabalho** — resolver o default, chamar o caso de uso, mapear o resultado — e `tests/test_ai_layer_boundaries.py` agora percorre `app/presentation/api/` para mantê-la assim. Nada na camada de roteamento importa SQLAlchemy, torch, transformers, PIL, langdetect, ou qualquer coisa chamada `clip`.
