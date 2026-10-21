# RFC-038 — Busca

**Status:** Implementado. A medição de §13 cumpriu os critérios de §8, a quente, na máquina do RFC-036: da tecla Enter à grade com 40 miniaturas em 544 ms, numa consulta em português; do clique em *"Mostrar mais"* à centésima miniatura em 425 ms, com o cache cheio; e da foto escolhida à grade em 507 ms. A ordem da grade foi a da resposta em todas as buscas, e nenhuma consulta chegou a um log. A medição achou um defeito, corrigido (§14.1): as fotos de DJI são MPO para o Pillow, e o upload as recusava. E a implementação achou que, no PostgreSQL 16 embarcado, o índice HNSW nunca serve a busca: ela é sempre exata, e o teto de 40 de §2.2 só existe no 17 (§14.1)
**Etapa:** 1 do [roadmap do frontend](ROADMAP-frontend.md#etapa-1--busca-rfc-038--qui-22-e-sex-23)
**Depende de:** RFC-037 (o cliente tipado, a barra e as pílulas já desenhadas, a ponte, o `shell.log`, os guardas do `Host` e do `Origin` e a CSP do build; [§14](rfc-037-fundacao-e-casca-do-frontend.md#14-o-que-passa-adiante)), RFC-026 (a rota e a resposta da busca), RFC-027 (o filtro por dispositivo), RFC-028 (o intervalo meio aberto, sem fuso, e o `excluded_unknown_date`), RFC-030 (o caminho na resposta, as miniaturas com `ETag`, o reveal atrás de dois guardas, e [§5.1](rfc-030-acesso-ao-arquivo.md#51-a-propriedade-de-segurança-nenhum-caminho-entra)), RFC-031 (§9 o `/capabilities`, §12 as correções do mockup), RFC-032 (§6 o círculo, o `min_radius_m` e o `excluded_unknown_position`), RFC-035 (o `execute_similar_to()` e a busca por imagem da CLI), RFC-036 (§5.2 o `search` do `/health`, §16 a primeira busca que espera o aquecimento)
**Bloqueia:** o RFC-040 (clicar numa célula do mapa abre o *"perto daqui"* deste RFC) e o corte 1 do roadmap, que troca o mapa pelo *"perto desta foto"* daqui
**Migration:** não
**Medição:** nenhum piloto. Os números citados vêm dos RFCs 023, 030, 032, 035, 036 e 037, cada um com a seção. A frase do pgvector de §2.2 vem do README dele, lido em 2026-10-20, e este RFC a cita sem tê-la medido. Os fatos sobre o código vêm da leitura do branch `frontend` em `24dc081`, e cada um diz o arquivo. A medição da implementação é `experiments/rfc-038-search/measure_search.py`, rodado em 2026-10-20 na mesma máquina, com o log ao lado (§13)

> **Um RFC curto, como o roadmap pediu** ([O prazo](ROADMAP-frontend.md#o-prazo-e-o-que-ele-obriga)): as decisões, os critérios de pronto com no máximo três números `TBM`, e o que ficou de fora. A convenção de rascunho continua valendo: todo número marcado `TBM` é *a medir*, e o que não é medição diz que é escolha ou hipótese.

---

## 1. Contexto

O RFC-037 pôs a tela inicial do Figma de pé com os números da API, e deixou a barra de busca, as pílulas e as consultas de exemplo desenhadas e desabilitadas (`frontend/src/home/Home.tsx`). A API já busca por texto com os três filtros, descreve uma foto, serve a miniatura com o disco na gaveta e abre o Explorer. Falta a interface de tudo isso, e falta a busca por imagem pela API: o caso de uso tem `execute_similar_to()`, e o único chamador é a CLI (`app/cli/search.py`).

Este RFC liga a barra, as pílulas e os filtros; desenha a grade de resultados e o detalhe da foto; e acrescenta à API as duas rotas da busca por imagem. Decide também três coisas que o roadmap não pedia e que a leitura do código mostrou necessárias: o `limit` acima de 40, o default do Explorer instalado e o que a interface pergunta ao `/capabilities` (§2).

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| A barra e a grade; cada cartão com a miniatura, o dispositivo, o nome do arquivo e o estado desconectado, sem score; os estados de vazio, sem resultados e modelo carregando | §5.3. O score entra, por decisão do produto (§2.10). O nome vem do caminho (§2.8) |
| Até 100 resultados, e *"Mostrar mais"* repetindo a busca com um `limit` maior | §5.3. Precisa de uma mudança no repositório (§2.2, §4.3) |
| A pílula *"Dispositivos"*, com `device_id` repetido | §5.2 |
| O intervalo de datas, com o aviso de `excluded_unknown_date` | §5.2, §5.3. O fim é exclusivo (§2.5) |
| *"Perto daqui"*, com o aviso de `excluded_unknown_position`; o raio em metros, no mínimo `min_radius_m`; o centro vindo de *"perto desta foto"* | §5.2, §5.4. O mínimo vem do `/capabilities` (§2.4) |
| O detalhe; *"Mostrar no Explorer"* só com `local_file_actions` e o disco conectado; o 409 e o 410 como mensagens; *"Copiar caminho"*, *"Parecidas com esta"* e *"Buscar perto daqui"* | §5.4. O Explorer não aparece instalado sem uma mudança de default (§2.3) |
| `GET /images/{id}/similar`, decidindo entre o embedding guardado e reabrir o arquivo | §4.1: o guardado, num caso de uso sem modelo (§2.1) |
| `POST /images/search/by-image`: os bytes no corpo, `image/jpeg` ou `image/png`, um limite de tamanho, um arquivo temporário na raiz de dados, nenhum caminho aceito, a rota literal antes de `/{image_id}` | §4.2 |
| Soltar uma foto na barra | §5.5. A CSP do build não deixa mostrá-la (§2.6) |
| *Critérios:* a ordem da grade é a da resposta; da tecla Enter à grade; a última de 100 miniaturas | §8. O de 100 precisa de mais de 45 fotos (§2.9) |
| *Riscos:* as datas sem fuso; a primeira busca esperando o aquecimento | §5.2, §5.3 |

## 2. O que o estado atual muda no roadmap

A leitura do código achou nove pontos, e uma decisão de produto veio depois (§2.10). Nenhum muda uma das decisões D1 a D5. Três acrescentam ao backend o que o roadmap não pedia (§2.2, §2.3 e §2.4), e um muda o que a medição precisa ter à mão (§2.9).

### 2.1 `execute_similar_to()` reabre o arquivo, e o provider dele carrega o modelo

O caso de uso entrega a foto ao adaptador, e o adaptador a abre pelo caminho absoluto: `_preprocess`, em `clip_embedding_model.py`, chama `require_absolute_path()`. Para uma foto indexada, isso tem três custos:

- **Com o disco na gaveta**, `require_absolute_path()` levanta, e a rota responderia 409, justamente no caso que o produto existe para atender (RFC-030 §4.1).
- **Com o disco plugado**, a foto é decodificada e passa pelo CLIP de novo, 0,464 s de média por imagem (RFC-023 §3), para produzir o vetor que já está na coluna `embedding`.
- **O provider da rota**, `get_search_images_use_case()`, depende de `get_embedding_model()`, cuja primeira chamada importa o `torch` (RFC-036 §5.3). Uma rota que não precisa de modelo esperaria a importação, de 13 a 18 s neste HDD (RFC-034 §9).

E o repositório não tem como devolver o vetor guardado: a porta `ImageRepository` não tem método que leia `embedding`, e o `search_similar()` o deixa fora do `SELECT` de propósito (`postgres_image_repository.py`).

**O `/similar` usa o embedding guardado, por um caso de uso novo, que não tem modelo** (§4.1). Funciona com o disco desplugado, com a busca em `loading` ou `unavailable`, e sem decodificar nada. A porta ganha um método de leitura, `get_embedding()`.

### 2.2 Uma varredura HNSW devolve no máximo 40 linhas

O README do pgvector, na pergunta *"Why are there less results for a query after adding an HNSW index?"*, responde: *"Results are limited by the size of the dynamic candidate list (`hnsw.ef_search`), which is 40 by default."* O RFC-025 §7.3 já registrou que a varredura explora no máximo `ef_search` candidatos, e o RFC-028 §8.1 fez a conta para um filtro. Ninguém a fez para o `limit`: todas as medições do planejador, nos RFCs 027, 028 e 032, usaram `limit = 10`, e o código não muda o `hnsw.ef_search` em lugar nenhum.

Com 45 fotos o planejador varre a tabela, e a ordenação é exata. Num acervo grande o bastante para ele escolher o índice (com 20 000 vetores, escolheu: RFC-032 §6.1.1), um `limit=100` sem filtro voltaria com no máximo 40, sem erro. O *"Mostrar mais"* do roadmap mostraria 40 e sumiria, porque um resultado curto é o sinal de que não há mais.

**Acima de 40, a busca pede ao pgvector uma lista de candidatos do tamanho do `limit`** (§4.3). Até 40, a consulta não muda nem num caractere.

### 2.3 O Explorer nunca aparece numa instalação

`allow_local_file_actions` vale `False` em todo modo (`settings.py`). O RFC-030 §6 o desligou seguindo o precedente do `warm_up_models`: *"o default seguro é o que não pode abrir janelas numa máquina que não pediu"*. O precedente mudou. O RFC-035 §8 pôs o `warm_up_models` em `MODE_DEFAULTS`, ligado no modo instalado, porque lá ninguém escreve o arquivo do usuário, e um `False` deixaria a API fria para sempre. O mesmo vale aqui: instalado, o botão *"Mostrar no Explorer"* não apareceria nunca. E o próprio RFC-030 §6 escreveu a regra: *"Um sistema que exige configuração para cumprir sua função principal está com o default errado."*

**`allow_local_file_actions` entra em `MODE_DEFAULTS`: ligado no modo instalado, desligado num checkout** (§4.4). O guarda de loopback do RFC-030 e os do `Host` e do `Origin` do RFC-037 continuam valendo nos dois.

### 2.4 A interface não tem como saber o raio mínimo

`min_radius_m` é um setting (300 m, `settings.py`), que o arquivo do usuário pode mudar, e nenhuma rota o publica: o OpenAPI não o tem, porque a rota recusa um raio pequeno com 400 em vez de declarar um `ge=` (RFC-032 §6). O mesmo vale para o `MAX_SEARCH_LIMIT`, que o RFC-026 §8.2 deixou só no caso de uso, de propósito. Copiar 300 e 100 para o frontend seria criar as cópias que o RFC-026 recusou.

**O `/capabilities` ganha os dois** (§4.4). Os dois passam pelo critério do RFC-031 §9, *"a interface desenha algo diferente por causa dele"*: o menor raio da lista, e o tamanho que o *"Mostrar mais"* pede.

### 2.5 O fim do intervalo é exclusivo

O roadmap registrou o risco do fuso: a API recusa `Z` e offset, e o `Date.toISOString()` acrescenta `Z`. Há um segundo. O intervalo é `[captured_from, captured_to)` (RFC-028), e *"2019-01-01 ends the year 2018"* (`images.py`). Quem escolhe *"até 31/12/2018"* espera as fotos do dia 31, e `captured_to=2018-12-31` as deixa de fora. **A interface manda o dia seguinte ao escolhido**, somado sobre os números da data, sem passar por um fuso (§5.2).

O mesmo cuidado vale para mostrar o `captured_at`, que vem sem fuso: `new Date("2018-07-14T15:32:05")` o lê como hora local, e uma hora que caiu num salto de horário de verão sai deslocada. **A interface formata a data a partir do texto**, sem `Date`.

### 2.6 A CSP do build não deixa a página mostrar a foto solta

O RFC-037 pôs `img-src 'self' data:` no `index.html` do build (`vite.config.mts`). A pré-visualização da foto solta na barra é um `URL.createObjectURL(file)`, que é uma URL `blob:`, e a CSP a recusa. Ler o arquivo para um `data:` funcionaria, ao custo de uma cópia em base64 de até 50 MB na memória da página. **`img-src` ganha `blob:`** (§5.6). Isso não abre nada para fora: um `blob:` só existe dentro da própria origem.

### 2.7 A consulta chegaria ao `shell.log` pelas URLs

O RFC-026 §16.1 decidiu que o texto da consulta não vai para log nenhum, e a rota cumpre (`images.py`). Mas este RFC põe a consulta na URL da tela (§5.1), e a casca grava URLs (`electron/main.ts`): o `did-fail-load`, que acontece em todo *"Reconectando…"*, escreve a URL da página; e o `console-message` escreve o `sourceId`, que num erro de rede é a URL do recurso, com o query string. **A casca nunca grava um query string**: uma função só, em `log.ts`, corta tudo a partir do `?` de toda URL que vai para o `shell.log` (§5.6).

### 2.8 O `filename` não tem a extensão, e as mensagens da API são em inglês

- O `filename` da resposta é o nome *"without its extension"* (`image_schema.py`). **O cartão mostra o último segmento do `relative_path`**, `DJI_0042.JPG`, que é o que o Explorer mostra.
- Os `detail` da API são as mensagens do domínio, em inglês (*"A search radius of 200 m is below the minimum…"*), e a interface é só PT-BR. **A interface escreve as próprias mensagens, por status e por rota** (§5.7), e mostra o `detail` da API só onde ele é o diagnóstico: o 503 de um modelo que não carrega, como o RFC-037 já faz com o `search_detail`.

### 2.9 A máquina das medições tem 45 fotos

O critério de 100 resultados do roadmap pede um acervo de mais de 100 fotos, e o banco das medições do RFC-037 tem as 45 imagens do RFC-022. **A medição indexa o dataset copiado três vezes, em três pastas, numa raiz de dados própria: 135 fotos** (§8). As cópias têm o mesmo embedding e o mesmo `ETag`, e ids e URLs diferentes, que é o que o tempo das miniaturas mede. Entre cópias iguais, a ordem é a do desempate por id (RFC-025).

### 2.10 O score no cartão desfaz uma decisão do roadmap

O roadmap e o RFC-031 §12 pedem *"nenhum score nos cartões"*, e o desenho do Figma o troca por três pontos de posição (§6). Quem mantém o projeto decidiu, em 2026-10-20, **mostrar o número da similaridade no canto do cartão**, sabendo que é mais difícil de o usuário entender e que é mais fiel ao que a busca calculou. Este RFC o registra como uma decisão de produto, e não como um erro do roadmap. O que o RFC-031 §12 tinha razão em tirar continua fora: **a porcentagem**, que daria a um cosseno não calibrado a aparência de uma probabilidade. O `ROADMAP-frontend.md` e o RFC-031 §12 precisam ganhar uma nota quando este RFC for implementado.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| `/similar` | O embedding guardado, por um `FindSimilarImagesUseCase`, que não tem modelo. A foto de origem sai do resultado, e as cópias dela ficam (§4.1) |
| A busca por upload | `POST /images/search/by-image`, com os bytes no corpo. O tipo é obrigatório (415), o tamanho tem teto de 50 MB (413), a imagem é decodificada antes do modelo (400), e o arquivo temporário fica em `cache\query-images`, na raiz de dados, apagado em toda saída (§4.2) |
| As respostas das duas rotas | A forma da busca sem o `query`: `RankedImagesSchema`, de que a `SearchResponseSchema` passa a herdar sem mudar (§4.1) |
| `limit` acima de 40 | `set_config('hnsw.ef_search', limit, true)` na transação da busca; até 40, nada (§4.3) |
| O `/capabilities` | Ganha `max_search_limit` e `min_radius_m` (§4.4) |
| O Explorer instalado | `allow_local_file_actions` em `MODE_DEFAULTS` (§4.4) |
| Os filtros | Uma dependência, declarada uma vez para as três rotas (§4.5) |
| A tela | `/busca`, com a busca inteira na URL, menos a foto solta. O detalhe abre num painel sobre a grade, que fica montada (§5.1) |
| A página | 40 resultados, e *"Mostrar mais"* pede o `max_search_limit`. A busca roda no Enter, e não a cada tecla (§5.2, §5.3) |
| As miniaturas | Todas pedidas logo (`loading="eager"`), com o espaço reservado. Um 404 é um quadro com o nome do arquivo, e não um erro (§5.3) |
| As datas | `AAAA-MM-DD`, o fim mais um dia, sem `Date`; o `captured_at` formatado do texto (§5.2, §5.4) |
| O raio | Escolhido numa lista que começa no `min_radius_m`; 1 km quando ninguém escolheu, sempre à vista no filtro (§5.2) |
| A casca | `blob:` na CSP; nenhum query string no `shell.log`; um marco por busca, pela ponte (§5.6) |
| As mensagens | Da interface, em PT-BR, por status e por rota (§5.7) |

## 4. O backend

### 4.1 `GET /api/v1/images/{id}/similar`

```text
GET /api/v1/images/{image_id}/similar?limit=40&device_id=…&captured_from=…&captured_to=…&near_lat=…&near_lon=…&radius_m=…
```

- **`FindSimilarImagesUseCase`**, em `search_images.py`, ao lado do `SearchImagesUseCase`, com o repositório, o `default_limit` e o `min_radius_m`, e sem modelo. A conferência do `limit`, a do raio e as duas contagens de excluídas passam a ser funções do módulo, que os dois casos de uso chamam: é a mesma política, e não pode haver duas.
- **O caminho:** `repository.get_embedding(id)`. Sem vetor, o caso de uso pergunta `exists()`: sem linha é o 404 de sempre (`ImageNotFoundError`); com a linha e sem embedding é 409 (`ImageNotIndexedError`, um `ConflictError`: o mesmo pedido funciona depois de indexar, que é a doutrina de `error_handlers.py`). Com o vetor, `search_similar(vetor, limit + 1, filtros)`, a foto de origem tirada pelo id, e o resultado cortado em `limit`. O resto é a ordem do repositório, intocada.
- **A foto de origem sai, e as cópias ficam.** Ela voltaria em primeiro, com similaridade 1, e ocuparia uma das 40 casas com a foto que o usuário acabou de ver. Uma cópia dela, em outro disco ou em outra pasta, tem o mesmo vetor e outro id, volta logo no começo, e responde *"onde mais está esta foto"*. A docstring de `execute_similar_to()` continua certa para o que ele faz: a CLI busca com um arquivo que pode não estar indexado.
- **`ImageRepository.get_embedding(id) -> EmbeddingVector | None`**: o vetor guardado, ou `None` sem linha ou sem embedding. Um `SELECT embedding` de uma linha. Nos três repositórios, com teste de contrato.
- **O provider não depende de `get_embedding_model()`**, como o do mapa (`get_aggregate_positions_use_case()`): compor o CLIP importaria o `torch` numa rota que nunca o usa. Um teste troca o provider do modelo por um que falha se for chamado.
- **A resposta** é uma `SimilarImagesResponseSchema`: um `RankedImagesSchema` com o `image_id` de origem. `RankedImagesSchema` é a `SearchResponseSchema` sem o `query` (`limit`, `results`, `excluded_unknown_date` e `excluded_unknown_position`, com as mesmas regras), e a `SearchResponseSchema` passa a herdar dela. A resposta da `/search` não muda.

**Rejeitado: o embedding numa subconsulta**, como o README do pgvector mostra para *"Get the nearest neighbors to a row"*. Economizaria uma ida e volta de uns 10 KB, o que em loopback com o PostgreSQL nativo não pesa, e criaria um segundo `SELECT` de ranqueamento, com os filtros, o desempate e o `ef_search` de §4.3, para manter igual ao primeiro. Ler o vetor e chamar o `search_similar()` é o caminho que os RFCs 025 a 032 mediram.

### 4.2 `POST /api/v1/images/search/by-image`

```text
POST /api/v1/images/search/by-image?limit=40&device_id=…
Content-Type: image/jpeg

<os bytes da foto>
```

- **O corpo são os bytes**, e não um `multipart/form-data`: não há outro campo a mandar, e o `python-multipart`, que o FastAPI pede para formulários, não está instalado. Os filtros vão no query string, com os nomes da `/search`. O `requestBody` do OpenAPI declara `image/jpeg` e `image/png`, para o `openapi-typescript` tipar a chamada.
- **O tipo é obrigatório: qualquer outro é 415.** Além de validar, isso fecha o formulário do RFC-037 §2.3 para esta rota. Um `<form>` só manda `application/x-www-form-urlencoded`, `multipart/form-data` ou `text/plain`, e um `fetch` de outra origem com `image/jpeg` dispara um preflight que a API não responde. O `Origin` continua conferido pelo middleware.
- **Os primeiros bytes têm de ser do tipo declarado**: `FF D8 FF` para o JPEG, e a assinatura de 8 bytes para o PNG. Senão, 415.
- **50 MB, e 413 acima.** Um `Content-Length` maior é recusado antes de ler; um corpo sem ele, ou que mente, é contado enquanto chega e cortado no teto. O teto é uma escolha: um JPEG de drone de 20 MP tem de 8 a 20 MB, e um PNG de uma tela 4K, uns 20. O corpo é lido numa dependência `async`, para a memória, porque escrever no disco dentro do laço de eventos seguraria o `/health`. A rota continua `def`, como as outras (RFC-026).
- **O arquivo temporário** é escrito em `cache\query-images`, na raiz de dados, com um nome aleatório, e apagado num `finally`, em sucesso, em recusa e em erro. A pasta é esvaziada na subida da API, para o que um processo morto deixou. O `cache` é o lugar do RFC-035 §5.4, em que nada é a fonte de nada, e o `%TEMP%` fica de fora pelo motivo de lá: o app guarda o que escreve na própria raiz de dados. Nenhum setting novo: o caminho vem do layout, como o do `huggingface` e o do `torchinductor` (`environment.py`).
- **A foto é decodificada antes do modelo.** Uma função de infraestrutura, `inspect_query_image()`, em `app/infrastructure/filesystem/`, abre o arquivo com o Pillow, confere que o formato é o declarado e que a imagem não passa de `Image.MAX_IMAGE_PIXELS` (89 478 485 pixels, o default do Pillow contra bombas de descompressão), e a decodifica. O que falhar é 400, com um `UnreadableQueryImageError`. Sem isso, um JPEG truncado chegaria ao `_preprocess` do adaptador, cujos erros passam adiante de propósito, e seria um 500. Decodificar duas vezes, aqui e no adaptador, é o preço, e entra no terceiro critério. O Pillow fica fora dos routers: `test_ai_layer_boundaries.py` proíbe o `PIL` em `app/presentation/api/`, e a dependência que chama a função mora em `app/presentation/`, como o `local_file_actions.py`.
- **A busca** é o `execute_similar_to()` de hoje, sobre o `Image` transitório que a CLI monta (`query_image()`, em `app/cli/search.py`), que passa para o caso de uso, para a CLI e a rota usarem o mesmo. Precisa do CLIP, e não do tradutor.
- **A resposta** é um `RankedImagesSchema`. A foto solta não está indexada, e não há o que tirar.
- **Nenhum caminho entra** (RFC-030 §5.1): o corpo são bytes, e o arquivo temporário é do servidor.
- **A ordem de declaração.** A rota fica ao lado de `/search`, antes de `/{image_id}`, como o roadmap pede. Hoje ela não colidiria: o único `POST` de dois segmentos é `/{image_id}/reveal`, que não casa com `search/by-image`. Fica ali pela leitura, e um teste a pede e exige o 200, como o `test_map_api.py` faz com o `/map`.

### 4.3 `limit` acima de 40

O `search_similar()` do PostgreSQL executa `SELECT set_config('hnsw.ef_search', :limit, true)` antes da consulta quando o `limit` passa de 40, o default do pgvector. O `true` vale até o fim da transação, que é a da requisição: o `get_db` fecha a sessão no fim dela (RFC-026 §7). Um `SET LOCAL` não serviria, porque não aceita parâmetro.

- **Até 40, nada é emitido**, e a consulta é a que os RFCs 025 a 032 mediram. É a regra 4 do RFC-032 §6.1: *"nenhuma mitigação nova pode mudar o caminho já validado"*. A primeira página da interface tem 40 resultados por isso (§5.3).
- **Não é um ajuste de recall.** É o mínimo para o `limit` poder ser atendido. O `ef_search` maior para os filtros continua sendo a decisão que o RFC-032 §6.1 deixou registrada, e uma busca filtrada com `limit=100` ainda pode voltar curta, pelo mecanismo do RFC-028 §8.1.
- **O custo não foi medido**: é `TBM` em §13, sobre o corpus de 20 000 vetores do `planner_check.py` do RFC-032.
- **A CLI** ganha o mesmo comportamento sem mudar, porque compõe o mesmo repositório.

### 4.4 O `/capabilities`, e o default do Explorer

| campo novo | valor | o que a interface desenha |
| --- | --- | --- |
| `max_search_limit` | O `MAX_SEARCH_LIMIT`, 100 | O tamanho que o *"Mostrar mais"* pede |
| `min_radius_m` | O `settings.min_radius_m`, 300 | O menor raio da lista |

E `allow_local_file_actions` passa para `MODE_DEFAULTS`, ao lado do `warm_up_models`: `True` com o layout instalado, `False` num checkout. O arquivo do usuário continua mandando nos dois. Num checkout, o `.env.example` já diz como ligar, e a medição liga pelo ambiente.

### 4.5 Os filtros, uma vez

O `limit` e os seis parâmetros de filtro da `/search` (`device_id`, `captured_from`, `captured_to`, `near_lat`, `near_lon` e `radius_m`) viram uma dependência só, que devolve o `limit` efetivo e o `SearchFilters`, e as três rotas a usam. As descrições dos parâmetros não mudam, e o snapshot do OpenAPI (RFC-037 §4.3) mostra se alguma mudou. O log de cada rota continua com o tamanho da consulta e as contagens, sem o texto e sem as coordenadas.

## 5. A interface

### 5.1 As telas e a URL

| URL | o quê |
| --- | --- |
| `/` | A tela inicial do RFC-037, com a barra, as pílulas e as consultas de exemplo ligadas |
| `/busca?q=…` | A busca por texto |
| `/busca?parecidas=<id>` | *"Parecidas com esta"* |
| `/busca?imagem` | A foto solta. Os resultados ficam na memória da página |
| `…&foto=<id>` | O detalhe, aberto sobre a grade |

Os filtros vão junto, com os nomes da interface, e uma função só os traduz para os da API: `dispositivo` (repetido), `de` e `ate` (`AAAA-MM-DD`, os dois inclusivos, §5.2), `perto` (`lat,lon`), `raio` (em metros) e `perto_de` (o id da foto que deu o centro, para o filtro dizer o nome dela).

**Por que na URL.** O voltar e o avançar funcionam; a busca sobrevive ao *"Reconectando…"*, que recarrega a página (RFC-037 §6.3); e a mesma busca é a mesma chave no cache do TanStack Query. A foto solta é a exceção: os bytes não cabem numa URL, e depois de uma recarga a tela pede para soltar a foto de novo.

**O detalhe é um painel sobre a grade**, e não uma tela: a grade fica montada, com a rolagem onde estava, e as 100 miniaturas não são pedidas de novo ao voltar. O `Esc` fecha.

### 5.2 A barra, as pílulas e os filtros

- **A busca roda no Enter**, ou num clique numa consulta de exemplo, e não a cada tecla: uma consulta em PT custa a tradução e o CLIP, 348 ms no backend (RFC-035 §15). Uma busca em andamento é cancelada pela seguinte, pelo `signal` do `openapi-fetch`. No backend ela termina, porque uma rota `def` não é interrompida, e a resposta é descartada.
- **Mudar um filtro roda a busca atual de novo**, seja qual for o tipo.
- **A pílula *"Dispositivos"*** abre a lista do `GET /devices`, conectados e desconectados, com uma caixa por dispositivo. Nenhuma marcada quer dizer todos, e não manda `device_id`; todas marcadas também não manda. O texto: *"Dispositivos · todos (4)"*, *"Dispositivos · HD3"* ou *"Dispositivos · 2 de 4"*. O `openapi-fetch` serializa um array como `device_id=A&device_id=B`, que é o que a rota espera, e um teste confere.
- ***"Filtros"*** abre a data e o *"perto de"*:
  - **A data:** dois campos `type="date"`, *"de"* e *"até"*, os dois inclusivos e cada um opcional. O valor de um `type="date"` já é `AAAA-MM-DD`. O *"até"* vai como o dia seguinte, somado sobre os números da data com `Date.UTC`, e nunca por `toISOString()`. Um *"até"* antes do *"de"* não é enviado, e o campo diz por quê.
  - **O *"perto de"*** só nasce de uma foto (§5.4). Aqui ele mostra o nome dela, o raio e *"remover"*. O raio é escolhido entre 300 m, 1 km, 5 km, 25 km e 100 km, numa lista que começa no `min_radius_m` do `/capabilities`, e é 1 km quando ninguém escolheu. **Não é o default que o RFC-032 §6 recusou**: a API não completa um raio que o cliente não mandou, porque não sabe o que ele quis; aqui o raio está escrito no filtro, e o que vai na requisição é o que está na tela. O 1 km é uma escolha, à vista do deslocamento aeronave–solo que o RFC-032 mediu: p95 de 274 m, máximo de 358 m.
- **Os filtros ligados** aparecem ao lado das pílulas, cada um como uma pílula escura com um ×, como no Figma (§6): *"Capturadas de 01/01/2018 a 31/12/2018"*, *"Até 1 km de DJI_0042.JPG"*. À direita fica a contagem, *"14 resultados · 0,13 s"*.
- **O estado da busca** ao lado da barra é o do RFC-037 §7. Com `unavailable`, a barra e a foto solta ficam desabilitadas, com o `search_detail`; *"Parecidas com esta"* continua funcionando, porque não usa modelo (§4.1).

### 5.3 A grade

**O cartão** (o desenho é o do nó `43:401`, §6): a miniatura, com o rótulo do dispositivo sobre ela no canto de cima à esquerda, e *"HD3 · desconectado"* no cinza quando `device.connected` é falso; abaixo, o nome do arquivo, o último segmento do `relative_path` (§2.8), e a pasta, o resto dele. Um cartão de disco desconectado tem o nome em tom mais fraco, e continua clicável. **No canto de cima à direita, a similaridade**, como número: o cosseno entre a busca e a foto, que a API já devolve, com três casas e vírgula, *"0,312"*, e não como porcentagem. Três casas, e não duas, porque os cossenos de um CLIP ficam numa faixa estreita e duas casas juntariam fotos que a ordem separa; é o formato da CLI (`format_hits()`). O número vem como a API o mandou, sem reescala nem corte (RFC-025 §14), e um negativo aparece com o sinal. Um `title` e o texto para leitor de tela explicam: *"Similaridade 0,312: o cosseno entre a sua busca e a foto, de −1 a 1. Só compara fotos da mesma busca."* O número é mais difícil de entender que os três pontos do desenho, e é mais verdadeiro: uma decisão de produto deste RFC, que **desfaz o *"nenhum score nos cartões"*** do roadmap e do RFC-031 §12 (§2.10). Um clique abre o detalhe. A grade é `auto-fill` de 240 px no mínimo, e a nota de rodapé do desenho fica: *"Ordenado por similaridade. O número é o cosseno entre a busca e a foto, não uma porcentagem, e só compara fotos da mesma busca."*

**A ordem é a da resposta.** O componente não ordena, não filtra e não agrupa: mapeia os `results` na ordem em que vieram. É o primeiro critério de §8.

**A página e o *"Mostrar mais"*:**

- A primeira página pede `limit=40`, explícito. O `top_k_results` (10) continua sendo o default de quem não manda o `limit`, como a CLI.
- *"Mostrar mais"* aparece quando vieram 40 de 40, e pede o `max_search_limit` de uma vez. A grade continua com os 40 enquanto os 100 não chegam (`placeholderData`), e depois mostra os 100, na ordem deles. Com o `ef_search` de §4.3, os 40 primeiros dos 100 podem não ser os 40 da primeira página; a grade mostra a resposta nova como ela veio.
- Menos que o pedido quer dizer que não há mais, e o botão não aparece. Numa busca filtrada sob o índice, isso pode ser falso (RFC-028 §8.1): o risco continua aberto, e a interface não tem como saber.

**As miniaturas:** `<img src="/api/v1/images/{id}/thumbnail" loading="eager" decoding="async">`, com a proporção do cartão reservada, para a grade não pular. Eager, e não lazy: são no máximo 100 arquivos de cerca de 45 KB num servidor em loopback, e uma miniatura que só é pedida quando aparece mediria a rolagem, e não a busca. O Chromium abre seis conexões por host em HTTP/1.1, que é o do uvicorn, e cada revalidação custa cerca de 6,6 ms no servidor (RFC-030 §7.2): as 100 entram numa fila de seis. Um 404 (*"no thumbnail yet"*, RFC-030) é um quadro com o nome do arquivo, e não um erro.

**Os estados:**

| quando | a grade mostra |
| --- | --- |
| Nenhuma busca ainda | A tela inicial |
| A busca em andamento, com o `/health` em `loading`; ou há mais de 1 s, com ele em `on_demand` | *"Carregando o modelo de busca. A primeira busca depois de abrir o SolidVision espera por ele."* O RFC-036 §16 mediu 15,0 s neste HDD |
| A busca em andamento, nos outros casos | Os resultados anteriores, esmaecidos, ou nada, na primeira |
| Nenhum resultado, e nenhuma foto indexada (a soma do RFC-037 §7) | *"Nenhuma foto indexada ainda."* |
| Nenhum resultado | *"Nenhuma foto encontrada."* Com filtros, *"…com esses filtros"*, e os avisos abaixo |
| `excluded_unknown_date` maior que 0 | *"N fotos ficaram de fora porque não têm data de captura."*, num aviso âmbar com o botão *"Tirar o filtro de datas"* (§6). Acima da grade, com resultados ou sem |
| `excluded_unknown_position` maior que 0 | *"N fotos ficaram de fora porque não têm posição."* Idem |
| Um erro | A mensagem de §5.7 |

Os dois avisos contam sobre o acervo inteiro, sob os outros filtros, e não sobre a página (RFC-028 §8.2). O texto não diz *"desta busca"*.

### 5.4 O detalhe

Abre com o que o resultado já trouxe e pede `GET /images/{id}` logo em seguida, porque o disco pode ter sido plugado ou tirado desde a busca.

| na tela | de onde, e como |
| --- | --- |
| A miniatura | A mesma URL do cartão. A foto em tamanho original está fora (roadmap) |
| A data de captura | O `captured_at`, formatado do texto: *"14/07/2018 às 15:32"*. A origem, do `capture_source`: *"pelo relógio da câmera"* (`exif_original`), *"data de digitalização"* (`exif_digitized`), *"o arquivo não tem data"* (`unknown`), *"ainda não lida"* (nulo) |
| A posição | `latitude, longitude` com seis casas, na ordem que a caixa de busca de um mapa aceita colada, como a CLI faz (`format_position()`). A origem: *"GPS do arquivo, onde estava a aeronave"* (`exif_gps`), *"o arquivo não tem posição"* (`unknown`), *"ainda não lida"* (nulo) |
| O dispositivo | O rótulo, e se está conectado |
| O caminho | Com o disco conectado, o `absolute_path`; senão, *"HD3 · fotos\2018\junho\DJI_0042.JPG"*. Com `\`, a forma do Windows: a API publica `/` (RFC-030) |

**As ações:**

| ação | quando aparece | o que faz |
| --- | --- | --- |
| *"Mostrar no Explorer"* | Com `local_file_actions` no `/capabilities` e o disco conectado | `POST /images/{id}/reveal`. O 204 não diz mais nada: o Explorer abre. O 409: *"O HD3 não está conectado. Conecte o disco e tente de novo."* O 410: *"O arquivo não está mais em F:\fotos\2018\junho."* O 404: o botão some, porque a configuração mudou |
| *"Copiar caminho"* | Sempre | Copia o caminho da tabela acima por `navigator.clipboard.writeText`, que a casca já permite (RFC-037 §16.2). Desconectado, o botão diz *"Copiar caminho no HD3"*, e copia o relativo |
| *"Parecidas com esta"* | Sempre | Abre `/busca?parecidas=<id>`, com os filtros atuais |
| *"Buscar perto daqui"* | Com posição | Aplica o círculo à busca atual, seja qual for o tipo, centrado na foto e com o raio de §5.2. Sem posição, o botão fica desabilitado, com a origem da posição ao lado |

### 5.5 Soltar uma foto

- **Na barra**, que mostra *"Solte a foto para buscar parecidas"* durante o arraste. Um botão na barra, *"Buscar por foto…"*, abre o seletor de arquivo, para quem não arrasta. Os dois seguem o mesmo caminho depois que o `File` existe.
- **A página cancela o `dragover` e o `drop` no `document` inteiro.** O padrão do Chromium é abrir na própria janela o arquivo solto fora de um alvo. O `will-navigate` do RFC-037 já recusa uma navegação para fora da origem, e cancelar no `document` evita que ela seja tentada.
- **Só JPEG e PNG**, pelo `type` do `File`. Outro tipo nem é enviado: *"Só dá para buscar com uma foto JPEG ou PNG."* Acima de 50 MB, também não: *"A foto passa de 50 MB."* A API confere as duas coisas de novo (§4.2), e é ela quem vale.
- **A barra mostra a foto solta**, pequena e com o nome, no lugar do texto, e um × que volta à tela inicial. A pré-visualização é um `blob:` (§2.6), revogado quando a foto sai da barra.
- **Os filtros valem** como em qualquer busca, no query string.
- **Nenhum caminho vai**: o corpo é o `File`. O `webUtils.getPathForFile()` do Electron daria o caminho, e não é usado (RFC-030 §5.1).

### 5.6 A casca

- **A CSP do build** ganha `blob:` em `img-src` (§2.6). Nada mais muda nela.
- **`redactUrl()`, em `electron/log.ts`**: toda URL que vai para o `shell.log` perde o query string e o fragmento. Passam por ela o `did-fail-load`, o `did-navigate`, o `will-navigate` e o `console-message`, no `sourceId` e no que o texto da mensagem tiver de URL (§2.7).
- **Um marco por busca**, pela ponte, como o `reportHome` do RFC-037 §6.5: `reportSearch({kind, limit, results, responseMs, paintedMs})`. Os tempos são medidos na própria página (`performance.now()`), do gesto (o Enter, o clique em *"Mostrar mais"*, a escolha da foto) à chegada da resposta e ao quadro seguinte à última miniatura decodificada. O marco não leva a consulta: só o tipo e as contagens. São os números de §8.

### 5.7 As mensagens

| status | rota | a interface diz |
| --- | --- | --- |
| 400 | `/search`, `/similar`, upload | *"A busca não pôde ser feita com esses filtros."* A interface não manda um raio abaixo do mínimo nem um intervalo invertido, e um 400 aqui é um defeito: o status vai para o console, e dali para o `shell.log`, sem o `detail` |
| 400 | upload | *"Não deu para abrir essa foto."* |
| 404 | `/similar`, detalhe | *"Essa foto não está mais no índice."* |
| 409 | `/similar` | *"Essa foto ainda não foi indexada."* |
| 409, 410 | reveal | As de §5.4 |
| 413, 415 | upload | As de §5.5 |
| 503 | `/search`, upload | O `detail`, que é a linha do RFC-035 §6.4: diz o modelo e o que fazer |
| Sem resposta | qualquer | O *"Reconectando…"* é da casca (RFC-037 §6.3). A tela guarda a última grade |

## 6. O Figma

O orçamento do roadmap é de seis a oito chamadas, e o RFC-037 gastou três. Este RFC gastou **duas**, em 2026-10-20, ao ser escrito: `create_session` e `get_design_context` no nó **`43:401`**, *"02 · Busca — resultados"*, que é a grade de resultados corrigida (4 × 3 cartões, a barra lateral com os quatro dispositivos, o aviso de datas e a nota de rodapé). Quem lê o RFC-038 não precisa de chamada nenhuma para a grade. O detalhe, os filtros abertos e a foto solta não estão no Figma, e são construídos no código com os mesmos tokens. O resultado do MCP é uma referência a adaptar, e não código a copiar (RFC-037 §8).

**O que o nó diz, e o que este RFC fez com cada ponto:**

| no nó | o que fica |
| --- | --- |
| Cartão de 261 px, a miniatura de 160 px de altura, um espaço de 18 px, quatro colunas na janela de 1440 px | A grade é `repeat(auto-fill, minmax(240px, 1fr))`, e não quatro colunas fixas: a janela do Electron tem no mínimo 960 px (`main.ts`) |
| O rótulo do dispositivo sobre a miniatura, no canto de cima à esquerda, com um ponto; *"HD3 · desconectado"* no cinza, e o nome do arquivo em tom mais fraco | §5.3: o rótulo vai sobre a miniatura, não abaixo dela |
| Abaixo da miniatura, o nome do arquivo e a pasta (*"fotos/2018/junho"*) | §5.3: o nome é o último segmento do `relative_path`, e a pasta é o resto dele |
| **Três pontos no canto de cima à direita**, com 1, 2 ou 3 acesos, só nos oito primeiros cartões | **Não entram: no lugar deles, o número da similaridade** (§5.3). Os pontos exigiriam faixas, ou seja, um corte que o RFC-025 §9.1 diz não existir, e escondem a diferença entre 0,31 e 0,29. O nó não define o estilo do número: ele usa o da nota de rodapé do desenho, no mesmo canto |
| Uma pílula escura por filtro ligado, com um ×: *"Capturadas em 2018 ✕"* | §5.2: os filtros ligados são essas pílulas |
| *"14 resultados · 0,13 s"*, à direita das pílulas | §5.3: a contagem vem da resposta, e o tempo é o de §5.6, medido na página, da requisição à resposta |
| O aviso âmbar *"112 fotos com embedding ficaram fora deste resultado: não têm data EXIF. **Incluir sem data**"* | §5.3: o texto sai sem a palavra *"embedding"*. *"Incluir sem data"* tira o filtro de datas: a API não tem como incluir fotos sem data num intervalo (RFC-028 §4.1), e o botão diz isso, *"Tirar o filtro de datas"* |
| A nota de rodapé *"Ordenado por similaridade — sem porcentagem…"* | Entra, com o texto de §5.3, que agora explica o número do canto. A segunda frase manda para *"Configurações › Sobre"*, que não existe no roadmap, e sai |
| A barra lateral, com a porcentagem e a data do scan de cada dispositivo, e *"+ Adicionar dispositivo"* e *"Configurações"* | É do RFC-039 |

## 7. Testes

**No backend, na suíte padrão:**

- **`/similar`:** 200 com o disco da foto desconectado, e com o provider do modelo trocado por um que falha se for chamado; a foto de origem fora, e uma cópia dela (o mesmo vetor, outro id) em primeiro; o `limit` cheio depois de tirar a origem; os filtros como na `/search`; 404 para um id desconhecido; 409 para uma linha sem embedding; 400 para `limit=101` e para um raio abaixo do mínimo.
- **`get_embedding()`**, no teste de contrato dos três repositórios: o vetor gravado, `None` sem linha e `None` sem embedding.
- **O `ef_search`:** com 150 vetores e o `enable_seqscan` desligado na transação, para o planejador usar o índice, `limit=100` sem filtro devolve 100; e o SQL emitido com `limit=40` não tem `set_config`. Pelo README do pgvector, sem a mudança voltariam 40. A implementação roda o teste antes da mudança, e o número que voltar vai para §13.
- **O upload:** 415 para `text/plain`, para `multipart/form-data` e para um PNG declarado como JPEG; 413 pelo `Content-Length` e por um corpo maior que o teto sem ele; 400 para bytes que não são imagem, para um JPEG truncado e para uma imagem acima do limite de pixels; 200 com um JPEG pequeno e o modelo de mentira; a `cache\query-images` vazia depois de cada um desses casos, e esvaziada na subida; o `requestBody` do OpenAPI com os dois tipos; a rota respondendo 200, e não 422.
- **O `/capabilities`** com os dois campos novos, e o `min_radius_m` acompanhando o setting.
- **O default do Explorer**: ligado com o layout instalado, desligado num checkout, e o arquivo do usuário mandando nos dois.
- **A `/search` não muda**: o snapshot do OpenAPI só muda nos acréscimos, e os testes dela passam sem edição.

**No `frontend/`, no `npm test`**, no ambiente `node` do Vitest, sem DOM:

- **A ordem da grade:** a grade renderizada por `react-dom/server`, a partir de uma resposta com as similaridades fora de ordem, e os `data-id` do HTML na ordem da resposta. É o primeiro critério.
- **As datas:** `de` e `ate` em `AAAA-MM-DD`; o fim mais um dia, inclusive em 31/12 e no fim de fevereiro de ano bissexto e não; nenhum `Z` nem offset em parâmetro nenhum; o `captured_at` formatado do texto.
- **A URL e a API:** a tradução dos nomes da interface para os da API, o `device_id` repetido, e uma URL sem filtros que não manda nenhum.
- **O raio:** a lista começando no `min_radius_m`, e o 1 km quando ninguém escolheu.
- **O número do cartão:** `0.3124` vira *"0,312"*; `-0.05`, *"−0,050"*; `1`, *"1,000"*; e o número exibido é o do `similarity` do mesmo resultado, não o de outro da lista.
- **O *"Mostrar mais"*:** aparece com 40 de 40; não aparece com 39 de 40 nem com 100.
- **As mensagens** de §5.7, por status e por rota.
- **`redactUrl()`**, e o `sourceId` e a URL do `did-fail-load` passando por ela.
- **A CSP do build** com `blob:` em `img-src`, e o resto dela igual.

## 8. Critérios de pronto

Medidos na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA), num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded`, `WARM_UP_MODELS=true` e `ALLOW_LOCAL_FILE_ACTIONS=true` no ambiente da janela, numa raiz de dados própria, com o dataset do RFC-022 copiado três vezes e indexado: 135 fotos (§2.9). A quente, como no RFC-036 §16, e com o `/health` em `search: ready` antes de cada medida: a primeira busca, que espera o aquecimento, já tem número (15,0 s, RFC-036 §16). Cada número é a mediana de cinco, lida dos marcos de §5.6. A janela é dirigida pelo protocolo de depuração do Chromium (`--remote-debugging-port`): a consulta e o Enter chegam como de um teclado, e a foto pelo seletor de arquivo (`DOM.setFileInputFiles`).

| critério | o número |
| --- | --- |
| **A ordem da grade é a da resposta** | Sem número. O teste de §7, e, na medição, os ids da grade lidos do DOM iguais aos da resposta, em toda busca |
| **Da tecla Enter à grade pintada com as 40 miniaturas**, numa consulta em português | `TBM` ms. No backend, a busca em PT leva 348 ms (RFC-035 §15) |
| **Com 100 resultados, do clique em *"Mostrar mais"* à centésima miniatura**, com as miniaturas no cache do Chromium, revalidando | `TBM` ms. Cada revalidação custa cerca de 6,6 ms no servidor (RFC-030 §7.2), e o Chromium abre seis conexões por host. Com o cache vazio, o número vai para §13, como informação |
| **Da foto escolhida à grade pintada com as 40 miniaturas**, com uma foto do dataset (1024 × 768) | `TBM` ms. O CLIP leva 0,464 s de média por imagem (RFC-023 §3), e a foto é decodificada duas vezes (§4.2). Com o maior JPEG à mão, o número vai para §13, com o tamanho dele |

E cinco verificações que não têm número:

- **Nenhuma consulta nos logs.** Depois da medição, o texto de cada consulta e as coordenadas de cada círculo, procurados no `shell.log` e no `api.log`: zero ocorrências (§2.7).
- ***"Parecidas com esta"* sem modelo.** Com `MODEL_RUNTIME_DIRECTORY` apontando para uma pasta vazia, a barra mostra o `search_detail` e fica desabilitada, e *"Parecidas com esta"* responde com resultados.
- **O upload recusado contra o app de verdade.** Por `curl`: 415, 413 e 400, e a `cache\query-images` vazia depois.
- **O Explorer.** O 204 abre a pasta da foto com ela selecionada; um arquivo da cópia renomeado durante a medição dá o 410 e a mensagem de §5.4. A medição abre janelas no desktop, a do SolidVision e a do Explorer, e quem a roda avisa antes.
- **A primeira busca durante o aquecimento** mostra o texto de §5.3 enquanto o `/health` diz `loading`, e a grade depois, sem erro.

## 9. Riscos

| risco | situação |
| --- | --- |
| Uma busca filtrada voltar curta sob o índice, e o *"Mostrar mais"* sumir com mais fotos existindo | Aberto desde o RFC-028 §8.1; o RFC-032 §6.1 decidiu não mitigar. §4.3 resolve só o `limit` sem filtro |
| O `ef_search` de 100 custar mais do que se espera | `TBM` em §13. Só as buscas acima de 40 pagam |
| A primeira busca esperar o aquecimento: 15,0 s neste HDD (RFC-036 §16) | A grade diz por quê (§5.3). Num checkout, com `on_demand`, a primeira busca carrega os dois modelos |
| O Explorer ligado por default na instalação | O guarda de loopback (RFC-030 §6) e os do `Host` e do `Origin` (RFC-037 §5.2) continuam, e o reveal pede um id que uma página não tem como saber. Um processo local continua sendo do token por sessão, que está fora |
| 50 MB não bastarem para alguém | É uma constante, e a mensagem diz o teto |
| Uma bomba de descompressão abaixo de 50 MB | O limite de pixels do Pillow, conferido antes do modelo (§4.2) |
| A foto solta perdida numa recarga | Declarado (§5.1): a tela pede para soltá-la de novo |
| A consulta chegar ao `shell.log` por um caminho que o `redactUrl()` não cobre | A primeira verificação sem número de §8 procura o texto das consultas nos dois logs |
| As cópias do dataset medirem miniaturas menores que as de um acervo real | As do dataset têm 512 × 384, uns 45 KB (RFC-030 §7.2). A de uma foto de 20 MP tem a mesma aresta de 512 px, e o mesmo JPEG |
| O link do nó da v2 não chegar | A grade sai dos tokens (§6) |

## 10. Fora deste RFC

- A foto em tamanho original (roadmap).
- A paginação, a rolagem infinita e mais que o `MAX_SEARCH_LIMIT`.
- Uma porcentagem, uma faixa por valor e um corte por similaridade mínima (o RFC-025 §9.1 diz o que falta para escolher um).
- O histórico de buscas, as buscas salvas e a busca enquanto se digita.
- Buscar com texto e foto juntos, ou com várias fotos.
- Soltar outros formatos (TIFF, BMP, WebP, HEIC) e colar uma imagem da área de transferência.
- Ir de uma foto à seguinte no detalhe.
- Mitigar a busca filtrada curta (RFC-028 §8.1, RFC-032 §6.1).
- Os cartões de dispositivo, as pastas e os jobs (RFC-039); o mapa e a CSP dos tiles (RFC-040).
- O token por sessão do RFC-036 §14, o tema escuro e outros idiomas.

## 11. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-038-busca.md` | Este documento |
| `backend/app/infrastructure/filesystem/query_image.py` | `inspect_query_image()`: o formato, os pixels e a decodificação (§4.2) |
| `backend/app/presentation/query_image_upload.py` | A dependência que lê o corpo, confere o tipo, o tamanho e a assinatura, e cuida do arquivo temporário (§4.2) |
| `backend/tests/presentation/test_similar_api.py`, `test_search_by_image_api.py` | §7 |
| `backend/tests/infrastructure/persistence/test_search_ef_search.py` | §7 |
| `frontend/src/search/` | A tela, a barra, as pílulas, os filtros, a grade, o detalhe e a foto solta, com os testes |
| `experiments/rfc-038-search/measure_search.py` (+ `.log`, fora do git) | §8, §13 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/application/use_cases/search_images.py` | O `FindSimilarImagesUseCase`; as conferências e as contagens como funções do módulo; o `Image` transitório, vindo da CLI |
| `backend/app/domain/repositories/image_repository.py`, e os três repositórios | `get_embedding()`; no PostgreSQL, o `ef_search` (§4.3) |
| `backend/app/domain/exceptions/` | `ImageNotIndexedError` (409) e `UnreadableQueryImageError` (400) |
| `backend/app/presentation/api/v1/routers/images.py` | As duas rotas, e os filtros como dependência |
| `backend/app/presentation/schemas/search_schema.py` | `RankedImagesSchema` e `SimilarImagesResponseSchema` |
| `backend/app/presentation/api/v1/routers/capabilities.py`, `schemas/capabilities_schema.py` | Os dois campos |
| `backend/app/presentation/dependencies/__init__.py` | O provider do `FindSimilarImagesUseCase`, sem modelo |
| `backend/app/presentation/api/__init__.py` | A `cache\query-images` esvaziada na subida |
| `backend/app/infrastructure/config/settings.py`, `layout.py` | `allow_local_file_actions` em `MODE_DEFAULTS`; o caminho da `query-images` |
| `backend/app/cli/search.py` | O `Image` transitório do caso de uso |
| `frontend/src/api/openapi.json`, `schema.d.ts` | Regenerados (`npm run api:types`) |
| `frontend/src/App.tsx`, `src/home/Home.tsx` | A rota `/busca`; a barra, as pílulas e as consultas de exemplo ligadas |
| `frontend/electron/log.ts`, `main.ts`, `preload.ts`, `src/shell/bridge.ts` | O `redactUrl()`; o `reportSearch` |
| `frontend/vite.config.mts` | `blob:` em `img-src` |
| `.env.example` | O comentário do `ALLOW_LOCAL_FILE_ACTIONS` diz o default por modo |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-frontend.md` | O status deste RFC |

Não mudam: as migrations, o launcher e o protocolo dos eventos, o `job_runner`, a resposta e os parâmetros da `/search`, o reveal, as miniaturas e o `docker-compose.yml`.

## 12. O que passa adiante

**Para o RFC-039 (dispositivos e indexação):** a pílula de dispositivos lê o mesmo `GET /devices` da barra lateral. O detalhe mostra o dispositivo da foto, e o caminho dele até a tela de pastas é desse RFC.

**Para o RFC-040 (mapa):** os filtros de dispositivo e de data, com os mesmos nomes na URL e a mesma tradução para a API (§5.1); o `blob:` já está na CSP, e os tiles são dele. **E uma lacuna:** o *"perto daqui"* deste RFC aplica um círculo a uma busca que já tem texto ou foto, e a `/search` exige o `q`. Uma célula do mapa não tem texto: o RFC-040 decide se ela abre a busca atual com o círculo, ou se pede uma rota que liste as fotos de um círculo sem ranqueamento.

**Para o RFC-041 (instalador):** a `cache\query-images` fica na raiz de dados, e a política de desinstalação a trata como o resto do `cache`; o Explorer ligado no modo instalado depende do layout que ele produzir.

## 13. Validação

Os `TBM` de §8 ficaram como foram escritos, à vista do que se esperava; os números que os respondem estão aqui, e o que mudou por causa deles, em §14.

**Declarado antes da medição**, em §8 e no script:

- **"A grade pintada"** é o quadro seguinte ao momento em que todas as miniaturas da resposta foram decodificadas (`img.decode()`), ou viraram o quadro de um 404. Medido na página, do gesto a esse quadro, e lido do marco `search` do `shell.log`.
- **As consultas** são cinco das 25 do RFC-022, em português: as traduções do RFC-035, a primeira de cada par, as mesmas em todas as rodadas. Uma sexta, fora da conta, aquece a janela.
- **"Com o cache cheio"** é a mesma busca repetida na mesma janela: as miniaturas voltam como 304. **"Com o cache vazio"** é o cache HTTP do Chromium esvaziado (`Network.clearBrowserCache`) antes de cada clique, em vez de um perfil novo: o efeito sobre as miniaturas é o mesmo, e o perfil da janela é o do desenvolvedor (RFC-037 §16.1).
- **A medição abre janelas no desktop**, a do SolidVision e a do Explorer, e isso foi dito antes de cada rodada.

> **Medido em 2026-10-20, na máquina do RFC-036**: Windows 10 Home (10.0.19045), Pentium G4560, 16 GB, HDD SATA, Python 3.12.9, Electron 44.9.0. Num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded`, `WARM_UP_MODELS=true` e `ALLOW_LOCAL_FILE_ACTIONS=true` no ambiente da janela, e uma raiz de dados própria num diretório temporário, apagado no fim. **O banco é o PostgreSQL 16.15 embarcado, com o pgvector 0.8.6**, e não o 17 do Docker, que estava desligado. **Um log** ao lado do script, fora do git: `measure_search.log`, a rodada completa. Quatro rodadas de depuração antes dela acharam um defeito do produto (as fotos de DJI recusadas, §14.1) e três do script (a saída em cp1252, o `document.body` nulo durante uma navegação, e as listas sem tipo); os números delas ficaram a menos de 60 ms dos da rodada final, e não entram aqui.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte do backend | `pytest` puro | **2854 passed, 91 deselected**, em 152,1 s, contra o PostgreSQL 16 embarcado, como o `.env` deste checkout manda. Eram 2780 no `24dc081`. Os 74 testes novos: a `/similar` (11), o upload (23), a decodificação da foto solta (6), o `FindSimilarImagesUseCase` (12), o `get_embedding()` nos três repositórios (9), o `ef_search` (5), os defaults do Explorer por modo (5), o `/capabilities` (1) e os dois erros novos no mapa de status (2) |
| As verificações do frontend | `npm test`, `npm run lint`, `npm run typecheck`, `npm run format:check` | **134 testes em 15 arquivos** (eram 77 em 6; o texto antigo da pílula saiu com o seu teste): as datas (9), a URL e a API (14), o raio (4), o número do cartão (3), os caminhos (4), as mensagens (8), a ordem da grade pelo `react-dom/server` (4), o *"Mostrar mais"* e os textos da tela (5), o `redactUrl()` e o marco (6) e a CSP (1). O ESLint, o Prettier e os dois `tsc` limpos |
| Estilo e tipos do backend | `black --check`, `ruff check`, `mypy` | **Limpos.** O `mypy` com a configuração do projeto, 337 arquivos; o script da medição, à parte |
| A ordem da grade | §8, em toda busca | **Os ids da grade, lidos do DOM, iguais aos da API, na ordem dela, em todas as 20 buscas por texto** (10 de 40 e 10 de 100), e na de *"perto daqui"* |
| Da tecla Enter à grade | §8 | **544 ms** (449 a 1160 ms), mediana de cinco, cada consulta a primeira vez na janela; **a resposta em 429 ms** (368 a 627 ms), contra os 348 ms do backend (RFC-035 §15). As mesmas cinco de novo, com as miniaturas no cache: 419 ms, a resposta em 393 ms |
| *"Mostrar mais"*, com o cache cheio | §8 | **425 ms** (392 a 454 ms) do clique à centésima miniatura. O botão some com 100 de 100, em todas |
| *"Mostrar mais"*, com o cache vazio | Informativo | **868 ms** (851 a 955 ms): as 60 miniaturas novas da rede, a 40 já na tela |
| Da foto escolhida à grade | §8, com a foto do dataset | **507 ms** (499 a 678 ms), com `fish_ponds_01.jpg`, 1024 × 768 e 198 KB; a resposta em 500 ms. A `cache\query-images` vazia depois das cinco |
| Da foto escolhida à grade, com a maior foto à mão | Informativo | **1102 ms** (1070 a 1304 ms), com `DJI_0321.JPG`, 4000 × 3000 e 6,3 MB, decodificada duas vezes (§4.2). Antes da correção de §14.1, recusada com um 400 |
| O `ef_search` | §7: o mecanismo, no servidor da suíte | **40 antes e 100 depois**: as linhas que uma varredura do índice HNSW devolve com `limit=100`, antes e depois de a busca emitir o `set_config` na transação. A busca em si devolveu 100 nos dois casos, porque no PostgreSQL 16 ela não usa o índice (§14.1). O corpus de 20 000 vetores e o p95 ficaram sem medição (§14.3) |
| Nenhuma consulta nos logs | §8 | **Zero ocorrências** das seis consultas digitadas (18 grafias: o texto, com `%20` e com `+`) e das duas coordenadas do círculo, no `api.log`, no `job_runner.log`, no `launcher.log` e no `shell.log`. As URLs que a casca gravou saíram sem o query string |
| *"Parecidas com esta"* sem modelo | §8: `MODEL_RUNTIME_DIRECTORY` numa pasta vazia | **A barra desabilitada com o `search_detail`** (*"The models are not in …"*), **e *"Parecidas com esta"* com 40 resultados.** Uma busca por texto pela URL mostrou a linha do 503 |
| O upload recusado | §8: `curl.exe` contra o app | **415** para `text/plain`, **413** para 51 MB declarados como `image/jpeg`, **400** para um JPEG cortado ao meio; a `cache\query-images` vazia depois |
| O Explorer | §8 | **O 204 abriu o Explorer em 1,3 s** (fechado pela medição); **um arquivo da cópia renomeado deu o 410**, e a tela disse *"O arquivo não está mais em C:\…\copia-c\images\aerial\rural."* O nome foi devolvido no fim |
| *"Perto daqui"* | Pela janela, com as 40 fotos de DJI indexadas depois das medições de tempo | **5 resultados a 1 km de `DJI_0344.JPG`**, iguais aos da API, e o filtro *"Até 1 km de DJI_0344.JPG"* na tela; `excluded_unknown_position` 135, as fotos do dataset, que não têm posição |
| A primeira busca durante o aquecimento | §8: o Enter logo depois da tela inicial | **O texto de §5.3 na tela com o `/health` em `loading`, e a grade 17,2 s depois do Enter, sem erro**; a resposta em 14,0 s. O RFC-036 §16 mediu 15,0 s |
| O Figma | §6 | **Duas chamadas**, feitas ao escrever o RFC. A implementação não gastou nenhuma |
| O build | `vite build` | **388,6 KB de JavaScript, 121,7 KB em gzip** (eram 359,4 e 112,7 KB), e 19,0 KB de CSS (4,0 KB) |

## 14. Correções feitas durante a implementação

Registradas à vista do texto original, como os RFCs 033 a 037 fizeram com as suas. Nenhuma muda uma das decisões de §3. Uma muda o que §2.2 supunha sobre o banco que vai na instalação, e fica registrada para o roadmap, sem conserto aqui.

### 14.1 O que a medição e o código contrariaram

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §2.2, §4.3 | Num acervo grande, o planejador escolhe o índice HNSW, e um `limit=100` sem filtro volta com 40 | **Depende da versão do servidor, e no PostgreSQL embarcado o índice nunca serve a busca.** O `search_similar()` ordena por `distância, id`, e o índice só dá a primeira chave. O PostgreSQL 17, onde o RFC-032 mediu (o do Docker), serve a primeira do índice e ordena os empates com um *Incremental Sort*; o 16, que é o `runtime\postgres` da instalação (ADR-008), não sabe fazer isso com um operador de ordenação, e varre a tabela. Conferido com `enable_seqscan` desligado: o `EXPLAIN` da consulta com o desempate dá `Seq Scan` com custo 10¹⁰, e a mesma consulta sem o `id` dá `Index Scan using ix_images_embedding_hnsw`. **No app instalado, então, a busca é sempre exata, e o teto de 40 de §2.2 não acontece**; o `set_config` de §4.3 vale para o PostgreSQL 17 do desenvolvimento e para o dia em que o runtime mudar de versão, e um teste (`test_which_plan_answers_the_search_depends_on_the_server`) avisa quando isso acontecer. **O outro lado é de desempenho, e não foi medido:** com 100 000 fotos, cada busca do app instalado é uma varredura de 100 000 vetores, e não as 2 a 3 ms do índice que o RFC-032 mediu no 17. Mudar a consulta para o 16 (um `LIMIT` interno só pela distância, e o desempate fora) é mudar o caminho validado, a regra 4 do RFC-032 §6.1, e é decisão de um RFC com o benchmark de escala |
| §7, §13 | O teste do `ef_search`: 150 vetores, o `enable_seqscan` desligado, e `limit=100` devolvendo 100 sob o índice | **No 16 a consulta da busca não usa o índice**, então o teste mede o mecanismo por uma consulta ordenada só pela distância, que os dois servidores atendem pelo índice: antes da mudança, **40** linhas com `limit=100`; depois, **100**, na mesma transação em que a busca emitiu o `set_config`. A busca em si devolveu 100 antes e depois, porque varreu a tabela |
| §7 | 150 vetores perto de um eixo | **A primeira geometria deu 33 linhas, fosse qual fosse o `ef_search`, num índice novo também.** Cada vetor saía do mesmo centro por um eixo diferente, uma estrela: a heurística de vizinhos do HNSW só guarda um vizinho mais perto do novo elemento do que dos já guardados, e na estrela todo ponto se liga só ao centro, cuja lista tem 32 vagas. Pontos aleatórios numa bola pequena, em volta de uma direção sorteada a cada teste, dão 40 e 100. A direção sorteada evita o outro efeito visto: a suíte insere e desfaz milhares de linhas nesse índice, e o grafo guarda as entradas abortadas até um vacuum, gastando o orçamento de candidatos, o mecanismo do RFC-025 §7.3 |
| §4.1 | *"A resposta da `/search` não muda"* | **O conteúdo não muda; a ordem das chaves, sim.** Herdando de `RankedImagesSchema`, os campos da base vêm primeiro, e o `query` passa a sair por último no JSON e na lista `required` do OpenAPI. Os mesmos membros, e os testes da `/search` passaram sem edição; o `openapi.json` mostra a troca |
| §4.1 | A foto de origem tirada pelo id, e o resultado cortado em `limit` | Como escrito. **Uma consequência:** a primeira página da `/similar`, de 40, pede 41 ao repositório, e no PostgreSQL 17 isso emite o `set_config` de §4.3. É o mínimo para o `limit` poder ser atendido, a mesma regra |
| §4.2 | O Pillow confere *"que o formato é o declarado"* | **Uma foto de DJI é `MPO` para o Pillow, e não `JPEG`**: um JPEG com uma segunda imagem dentro, a prévia (o Multi-Picture Format da CIPA). As 40 fotos reais de DJI do RFC-032 abrem como `MPO`, e a primeira versão da conferência recusou todas com um 400, *"Não deu para abrir essa foto"*: justamente as fotos que o produto existe para achar. A medição achou, na rodada de depuração; `image/jpeg` aceita `JPEG` e `MPO`, e um teste salva um MPO e o manda como JPEG. O adaptador lê o primeiro quadro, que é a foto |
| §8 | *"Perto daqui"* exercido com o dataset | **As 45 imagens do RFC-022 não têm GPS**, e o botão fica desabilitado em todas. A medição indexa também a pasta de fotos reais de DJI do RFC-032 (40 arquivos, só lidos) para exercer o círculo pela janela, e para o *"maior JPEG à mão"* |

### 14.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §4.2 | **O `openapi-typescript` tipa um `format: binary` como `string`**, e o cliente tipado recusaria o `File`. O `schema-types.mjs` ganhou um `transform` que o tipa como `Blob`; o `npm test` confere o `schema.d.ts` pela mesma função |
| §5.1 | **Um gesto refaz a busca; voltar, não.** As buscas ficam no cache para sempre (`staleTime: Infinity`), para o voltar, o avançar e a recarga do *"Reconectando…"* serem imediatos; um Enter, um filtro, *"Parecidas com esta"* ou *"Buscar perto daqui"* marcam a busca como velha antes de navegar. Sem isso, digitar de novo uma consulta já feita mostraria o cache sem perguntar nada à API, com os discos plugados ou não como estavam |
| §5.7 | **Sem resposta, a tela guarda a última grade** lendo a última página boa do próprio cache, esmaecida, com *"Sem resposta do SolidVision…"* em cima |
| §2.4, §4.4 | **O `allow_local_file_actions` mudou de default**, e dois testes que o liam mudaram com ele: o do default, que agora confere o do checkout pelo `MODE_DEFAULTS`, sem o `.env`; e o de `/capabilities` que contava três campos, agora cinco |
| §5.3 | **O aviso âmbar não tem valores no RFC.** O nó `43:401` desenha a cor sem variável, e as duas chamadas de §6 não deixaram os números escritos; os tokens `--color-warning-*` foram escolhidos na mesma família, sem chamada nova ao Figma |
| §5.2 | **As datas se aplicam num botão** (*"Aplicar"*), e não a cada mudança do campo: um `type="date"` do Chromium dispara uma mudança a cada parte digitada, e cada uma seria uma busca com um ano como `0002` |
| §5.4 | **O detalhe aberto da grade fecha com um *voltar***, e um aberto por uma URL, com um `replace`: um detalhe que veio de uma recarga não pode, ao fechar, tirar o usuário do app |
| §5.6 | **O tempo ao lado das pílulas é o da requisição**, medido no `queryFn`; o do marco começa no gesto. Os dois dizem coisas diferentes, e §5.2 e §5.6 os separavam sem dizer |
| §2.7 | **O `redactUrl()` deixa um `?…` no lugar do que cortou**, para o log mostrar que havia um query string sem mostrar qual |

### 14.3 O que ficou sem medição

- **O custo da varredura sequencial num acervo grande** no PostgreSQL 16 (§14.1). É a pergunta que o benchmark de escala precisa responder antes de a instalação ter 100 000 fotos.
- **O custo do `ef_search` de 100 no índice** (§9, segunda linha), que §13 pedia sobre o corpus de 20 000 vetores do RFC-032: esse corpus foi medido no PostgreSQL 17 do Docker, que estava desligado, e no 16 a busca não usa o índice. Só as buscas acima de 40 pagariam, e só no 17.
- **Soltar uma foto arrastando**: a medição escolhe a foto pelo seletor (`DOM.setFileInputFiles`); o arraste chama a mesma função depois que o `File` existe.
