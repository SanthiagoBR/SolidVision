# RFC-044 — Fotos por Localização, sem Consulta Textual

**Status:** Implementado, com as duas recomendações de §3 como estão e sem índice: o critério de §9 não o justificou. A medição de §15, na máquina do RFC-036, com 100 000 fotos, deu a primeira página do lugar mais cheio — 6 076 fotos num círculo de 1 km — em 49 ms no servidor e 142 ms pela API, com o resumo; do clique no balão à grade, 409 ms; e *"Mostrar mais"* em 108 ms de resposta e 1 120 ms até as 100 miniaturas. Achou dois defeitos latentes do balão do mapa, do RFC-040, corrigidos (§16.1). A segunda versão do rascunho, do mesmo dia, está registrada em §14
**Depende de:** RFC-025 (§4.1, `SearchHit`, cuja `similarity` obrigatória é o motivo de este RFC não reaproveitá-lo; §6, o desempate por `id`), RFC-027 (o filtro por dispositivo), RFC-028 (§4.1, uma data desconhecida nunca "combina"; §8.1, o B-tree em `captured_at` medido e não criado; §8.2, o `excluded_unknown_date`), RFC-030 (§5.1, nenhuma rota aceita um caminho; as miniaturas com o disco na gaveta), RFC-032 (§6, o círculo e o `min_radius_m`; §6.2; §7, o universo do mapa e o pedido de N + 1; §11 item 1, que pediu exatamente este RFC), RFC-033a (§5, a localidade `C` nos dois bancos), RFC-038 (§4.1, o provider sem modelo; §4.5, os filtros declarados uma vez; §5.1, a busca na URL; §5.3, a grade e o *"Mostrar mais"*; §14.1, o PostgreSQL 16 sem o índice HNSW), RFC-040 (§2.1, a lacuna; §5.4, o balão e o raio que cobre a célula; §13 e §14.1, os custos com 100 000 fotos), RFC-043 (as coordenadas e o endereço como portas do *"Perto de"*)
**Bloqueia:** nada
**Migration:** nenhuma. O B-tree em `captured_at` não passou no critério de §9 (§15)
**Medição:** `experiments/rfc-044-browse/measure_browse.py` (+ `.log`, fora do git), rodado em 2026-10-22: o servidor, pelo `EXPLAIN` das consultas do repositório; a página, num Edge headless; e as seis verificações de §9 (§15). As seções 1 a 14 são o rascunho como foi aprovado; o que a implementação mudou nele está em §16

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir*, e **todos viraram número** em §15. As seções 1 a 14 ficam como o rascunho as deixou: reescrevê-las apagaria o porquê de cada critério.

---

## 1. Contexto

Caso de uso real, descrito pelo usuário: um cliente do profissional que usa o SolidVision pede para fotografar uma construção até ela ficar pronta — meses de fotos do mesmo lugar. Hoje, a única forma de ver "tudo que existe perto deste lugar" é buscar um texto e filtrar por "Perto de" (RFC-032, RFC-038 §5.2): a busca por *"construção"* é ranqueada por semelhança com essa palavra, e um prédio terminado — uma fachada pronta, não mais um canteiro de obras — pode não ranquear bem o suficiente para aparecer, mesmo estando exatamente no círculo certo. A ferramenta que existe responde "o que parece com X aqui", e a pergunta real é "o que existe aqui, no tempo".

O quanto ele some depende do tamanho do círculo. No PostgreSQL 16 embarcado a busca com um círculo é exata (RFC-038 §14.1): ranqueia todas as fotos do círculo e devolve as 40 primeiras, ou 100 com *"Mostrar mais"* (RFC-040 §2.1). Com até 40 fotos ali, o prédio pronto aparece, fora da ordem do tempo; com mais de 100, pode não aparecer em página nenhuma. Meses de visitas ao mesmo lugar são o segundo caso.

O RFC-032 §11 já tinha nomeado isto como o primeiro item de trabalho futuro, antes de o mapa existir:

> *"Listar fotos de uma célula do mapa, sem consulta textual. É o primeiro clique que a UI de mapa vai querer e este RFC não entrega. Precisa decidir ordenação (data de captura, presumivelmente) e paginação — RFC próprio."*

O mapa (RFC-040) chegou e confirmou a lacuna pelo outro lado: hoje, "Buscar perto daqui" sem nenhuma busca ativa leva à tela inicial com o círculo pronto e a barra com foco — esperando um texto que, neste caso de uso, não existe para digitar. E desde o RFC-043 o *"Perto de"* tem quatro portas — uma foto, uma célula do mapa, coordenadas digitadas e um endereço —, e as três últimas terminam no mesmo lugar: a tela inicial com um círculo e sem texto.

**O que o pedido pede, e onde está neste documento:**

| o pedido | onde |
| --- | --- |
| Ver tudo o que existe perto de um lugar, sem digitar uma descrição | §6.2: uma ação ao lado da busca, nas três telas que já têm um círculo |
| Na ordem do tempo, *"até ela ficar pronta"* | §3.1 e §5.2: pela data de captura, as mais recentes primeiro, com *"Mais antigas primeiro"* a um clique |
| Meses de fotos, e não uma página | §2.1 e §5.2: páginas por cursor, até a última |
| Uma foto daqui sem data | §3.2: no fim da lista, depois de um divisor |
| Funcionar sem o modelo da busca | §5.4: a rota nova não carrega o CLIP |

## 2. O que a leitura do código muda no rascunho

A primeira versão deste RFC acertou a forma — uma rota nova, um contrato sem `similarity`, os filtros da busca — e deixou três coisas para depois, que a leitura do código mostra que não podem ficar. Os pontos abaixo dizem o arquivo de cada fato.

### 2.1 O teto de 100 corta o meio da obra

O rascunho deixou a paginação real `TBM`, *"até alguém medir quantas fotos uma obra típica gera"*. A conta não depende dessa medição. Com o `MAX_SEARCH_LIMIT` de 100 (`search_images.py`) e o botão de inverter a ordem (§3.1), a lista mostra as 100 mais recentes e as 100 mais antigas. Com 300 fotos no círculo, as 100 do meio — os meses em que a obra mais mudou — não aparecem em nenhum dos dois sentidos. E as fotos deste acervo se concentram: no piloto do RFC-032, 5 células de ~100 m guardavam 45% das posições (RFC-032 §6.1). Um lugar visitado toda semana durante meses é essa concentração no extremo.

**O teto continua, por página.** Ele existe para que um pedido não serialize uma lista sem fim (o comentário do `MAX_SEARCH_LIMIT`), e isso não muda. O que muda é que **há uma página seguinte**: `after=<id>`, a última foto da página anterior (§5.2).

- **Rejeitado: `offset`.** A lista é lida enquanto um job indexa (RFC-039), e uma foto nova antes da posição lida desloca as seguintes: o `offset` repete uma e pula outra, sem erro. O cursor continua de onde parou. E um `offset` alto ordena `offset + limit` linhas a cada página; o cursor, `limit + 1`.
- **Rejeitado: esperar a medição de uma obra real.** Ela diria quantas páginas uma obra tem, e não se há mais de uma. O número continua útil para o risco de §10 (uma grade com milhares de cartões), e não decide nada aqui.

### 2.2 Uma foto daqui sem data, contada e sem caminho até ela

O rascunho tirava a foto sem data da lista e a contava, *"no mesmo padrão de `excluded_unknown_date` (RFC-028 §4.1) e `excluded_unknown_position` (RFC-032 §6.2)"*. Mas os dois avisos da busca têm saídas diferentes. O de datas tem *"Tirar o filtro de datas"*, que traz as fotos de volta (RFC-038 §5.3). O de posição não tem botão — *"não há como incluí-las"* (RFC-040 §5.5) — e não precisa, porque uma foto sem posição não é "daqui". **Uma foto sem data é daqui**: tem posição, e está dentro do círculo. Um aviso dizendo *"12 fotos daqui não têm data"* sem um caminho até elas seria o único beco da interface: o aviso da posição sem a desculpa dele.

E a reutilização que o rascunho supunha não existe para esse caso. `count_unknown_capture_date()` responde 0 sem um intervalo de datas, e sem consultar nada (`postgres_image_repository.py`); `count_hidden_by_unknown_date()` responde `None` antes de chamá-la (`search_images.py`). Os dois são assim de propósito: sem intervalo, não houve filtro de data para esconder nada (RFC-028 §8.2). Uma listagem que tirasse as fotos sem data *sempre* precisaria de outra contagem, com outro significado para o mesmo campo.

§3.2 resolve os dois pontos com uma decisão só: sem filtro de datas, a foto sem data está na lista, no fim.

### 2.3 No PostgreSQL 16, a busca não atravessa índice nenhum

O rascunho dizia que a listagem *"deve ser mais barata que a busca por texto equivalente, por não ter inferência nem travessia de índice vetorial"*. A segunda metade não vale para a instalação: no PostgreSQL 16 embarcado o índice HNSW nunca serve a busca, que varre a tabela calculando o cosseno de cada linha (RFC-038 §14.1). A diferença real é outra, e é maior:

- **Sem o CLIP**: a tradução e a codificação de uma consulta em português custam 348 ms no backend (RFC-035 §15), e a primeira busca depois de abrir o app espera o aquecimento (RFC-036 §16).
- **Sem os vetores**: a listagem lê as colunas da entidade, como a busca (`search_statement()`), e não o `embedding`. Os vetores ficam no TOAST, que uma consulta que não os pede não toca: com 100 000 fotos, 16 MB de heap contra 294 MB com o TOAST (RFC-040 §13).

E o rascunho temia que *"um `ORDER BY` sem esse filtro [pudesse] usar um índice para evitar um *sort* em disco"*. Com um `LIMIT`, o PostgreSQL ordena por um *top-N heapsort*, que guarda só as `limit + 1` primeiras linhas na memória — uma hipótese, conferida no `EXPLAIN` de §9. A ordenação que foi para o disco no RFC-040 §14.1 era a do `GROUP BY` do mapa, de 89 883 linhas e sem `LIMIT`. **A pergunta do índice é a da varredura, e não a da ordenação.** Um B-tree em `captured_at` deixaria um filtro pouco seletivo — um disco inteiro — ler as primeiras linhas já em ordem e parar; com um círculo, a varredura é a da distância haversine sobre a tabela, que o RFC-032 §6.1 mediu sem índice espacial e decidiu não indexar. O critério do índice está em §9.

### 2.4 O cursor não pode ser um caminho

O cursor natural de uma lista ordenada é a chave de ordenação da última linha: aqui, a data, o caminho relativo e o id (§5.2). Mas nenhuma rota aceita um caminho de volta, nem codificado (RFC-030 §5.1, `image_schema.py`: *"No route accepts a path back"*). **O cursor é o id da última foto**, e o servidor lê a chave dela pela chave primária (`ImageRepository.get()`): uma leitura a mais por página, de uma linha.

### 2.5 O círculo não é a célula

O balão conta as fotos de uma célula, que é um quadrado (`aggregate_statement()`); o *"Buscar perto daqui"* dele usa o menor raio da lista que cobre a célula, um círculo de ~2,8 vezes a área dela, com fotos das vizinhas (RFC-040 §5.4 e §9). Listar exatamente a célula pediria um filtro por caixa, que o RFC-032 §9 recusou. **A listagem é a do círculo**, o filtro que as quatro portas do *"Perto de"* já produzem. O balão diz *"37 fotos nesta célula"*, e a listagem pode dizer *"52 fotos daqui"*: a tela diz o total do círculo e o filtro diz o raio, e o botão do balão não repete o número da célula (§6.2).

### 2.6 O universo é o do mapa

A busca, as duas contagens e o mapa contam só as fotos com embedding (`embedding IS NOT NULL`, RFC-032 §7). A listagem também, para que o balão e a listagem contem a mesma população, a menos da diferença entre o quadrado e o círculo. Uma linha sem embedding — gravada pelo `save()` do RFC-015 — não aparece, como não aparece no mapa.

### 2.7 A grade supõe a similaridade

O tipo do resultado tem `similarity`, e a grade a desenha no canto do cartão (`ResultGrid.tsx`, `formatSimilarity()`); a nota de rodapé diz *"Ordenado por similaridade"* (`GRID_FOOTNOTE`, em `similarity.ts`); e o *"Mostrar mais"* troca os 40 pelos 100, em vez de acrescentar (`SearchScreen.tsx`). A listagem usa a mesma grade, com o canto do cartão como um espaço que cada tipo preenche, a sua nota e o seu *"Mostrar mais"* (§6.3).

### 2.8 Os filtros já estão prontos para outra consulta

Este ponto confirma o rascunho. `_apply_filters()` é um método estático que aplica o dispositivo, o intervalo e o círculo a qualquer `Select` (`postgres_image_repository.py`); `search_parameters()` declara o `limit` e os seis filtros uma vez só, para as três rotas da busca (`images.py`, RFC-038 §4.5); e `validated_request()` e as duas contagens são funções do módulo `search_images.py`. O trabalho novo é o `ORDER BY`, o cursor e o resumo.

## 3. As duas perguntas de produto

Duas perguntas que nenhuma linha de código responde. Cada uma tem uma recomendação, a confirmar com quem usa o acervo real.

### 3.1 A ordem: as mais recentes primeiro, com a opção de inverter

**Recomendação:** mais recente primeiro. É a convenção de toda galeria — inclusive a grade atual do SolidVision, cuja ordem é a da resposta, sem inversão (RFC-038) — e responde primeiro a "como está agora". A narrativa inversa ("do buraco no chão ao prédio pronto") é *"Mais antigas primeiro"*, um botão na mesma tela, e não um modo separado: é a mesma consulta, com o sentido da data trocado.

**A primeira versão entrega os dois sentidos.** O rascunho deixava isso `TBM`; com o cursor de §5.2, o sentido é um parâmetro da mesma consulta, e o teste do contrato é o mesmo nos dois.

### 3.2 A foto sem data: no fim da lista, depois de um divisor

RFC-020 e RFC-028 decidiram que uma data desconhecida nunca "combina" com um filtro de intervalo. Numa *ordem* cronológica a mesma foto não tem posição — mas "nunca aparece" e "nunca combina" não são a mesma garantia.

**Recomendação:**

- **Sem filtro de datas, as fotos sem data vêm depois de todas as datadas, nos dois sentidos**, na ordem das pastas, sob um divisor: *"Sem data de captura · 12 fotos"*. Nenhuma foto sem data fica entre duas datadas: a linha do tempo continua pura, e acaba onde as datas acabam.
- **Com um filtro de datas, ficam de fora e são contadas, como na busca** (RFC-028 §4.1): `excluded_unknown_date`, o aviso âmbar e *"Tirar o filtro de datas"* — que as traz de volta, para o fim da lista.

As duas garantias valem juntas: com um intervalo, uma data desconhecida nunca combina; sem ele, uma foto daqui nunca some. A proposta do rascunho — sempre de fora, sempre contada — fica registrada em §14, com o motivo da troca (§2.2).

### 3.3 Um contrato novo, e não a busca com a palavra vazia

Uma restrição que o código já impõe, por decisão deliberada do RFC-025: `SearchHit.similarity` é um `float` obrigatório, documentado assim —

> *"Uma similaridade não significa nada sem a busca que a produziu (...) e inventar uma pontuação para ela [a devolveria] como se tivesse sido comparada."*

Isto não é um detalhe de tipagem: é a razão pela qual **esta listagem não pode ser "a busca de sempre, com a palavra vazia"**. Precisa do seu próprio contrato, sem um número que finge ser uma pontuação. O RFC-040 §2.1 chegou ao mesmo lugar pelo outro lado, ao recusar o `q` opcional na `/search`: *"a rota passaria a responder duas perguntas com a mesma forma"*.

## 4. Decisão

| decisão | resultado |
| --- | --- |
| A rota | `GET /api/v1/images/browse`, com os filtros da busca pela mesma dependência, `order` e `after`; declarada antes de `/{image_id}` (§5.1) |
| A resposta | As fotos na forma do `GET /images/{id}` (`ImageSchema`), sem `similarity`; `next_after`, exato; e um resumo da lista, só na primeira página (§5.1, §5.3) |
| A ordem | A data de captura no sentido pedido, sem data por último nos dois sentidos, e o caminho relativo e o id como desempate, sempre crescentes (§5.2) |
| A paginação | Por cursor: o id da última foto, lido de volta pela chave primária. Até 100 por página, como a busca (§2.1, §2.4, §5.2) |
| A foto sem data | Na lista, no fim, sem filtro de datas; com ele, de fora e contada, como na busca (§3.2) |
| O caso de uso | `BrowseImagesUseCase`, sem modelo, com a conferência e as contagens da busca (§5.4) |
| O repositório | `list_by_filters()` e `summarize_listing()`, nos três, com teste de contrato (§5.4) |
| O índice | Nenhum, a menos que o critério de §9 o justifique |
| A interface | Um quarto tipo de busca na URL, `todas`; *"Ver todas as fotos daqui, por data"* na tela inicial com um círculo, no balão do mapa e na busca com um círculo; a data no canto do cartão; o divisor das sem data; *"Mostrar mais"* que acrescenta (§6) |

## 5. O backend

### 5.1 `GET /api/v1/images/browse`

```text
GET /api/v1/images/browse?order=newest&limit=40&near_lat=-26.3214&near_lon=-48.8163&radius_m=1000
GET /api/v1/images/browse?order=newest&limit=100&after=<id>&near_lat=-26.3214&near_lon=-48.8163&radius_m=1000
```

**O nome é o candidato do rascunho.** A rota lista, e a ordem é um parâmetro: outra ordem, a das pastas, caberia nela sem outro nome. *"timeline"* amarraria o nome à data.

| parâmetro | o quê |
| --- | --- |
| `order` | `newest`, o padrão, ou `oldest`. Outro valor é 422, como qualquer enum do FastAPI |
| `after` | O id da última foto da página anterior; ausente na primeira. Um texto que não é UUID é 422 |
| `limit`, `device_id`, `captured_from`, `captured_to`, `near_lat`, `near_lon`, `radius_m` | Os da busca, pela mesma dependência, `search_parameters()`, com as mesmas regras e os mesmos 400: o `limit` de 1 a 100, o padrão `top_k_results`, o círculo tudo ou nada e acima do `min_radius_m`, o intervalo sem fuso (RFC-026, 028, 032, 038 §4.5) |

| campo da resposta | o quê |
| --- | --- |
| `order` | O sentido aplicado |
| `limit` | O tamanho de página aplicado, como na busca |
| `results` | As fotos, na ordem de §5.2, cada uma na forma do `GET /images/{id}` (`ImageSchema`): o dispositivo e se está conectado, os caminhos, a data e a posição com as fontes. Nenhuma `similarity` |
| `next_after` | O id a mandar como `after` para a página seguinte, ou `null` quando a lista acabou. Exato: a consulta pede `limit + 1` linhas, como o mapa pede `MAX_MAP_CELLS + 1` células (RFC-032 §7), e não *"menos que o pedido quer dizer que não há mais"* |
| `summary` | O resumo da lista inteira (§5.3) na primeira página, e `null` nas seguintes |

| status | quando |
| --- | --- |
| 200 | Uma página, vazia inclusive |
| 400 | Os da busca: `limit` fora da faixa, raio abaixo do mínimo, círculo incompleto, intervalo invertido ou com fuso |
| 404 | `after` não é o id de nenhuma foto: a última da página anterior saiu do índice. É o `ImageNotFoundError` de sempre |
| 422 | `order` fora do enum; `after` ou `device_id` que não são UUID; um número que não é número |

Nenhum 503: a rota não usa modelo.

- **Declarada antes de `/{image_id}`**, ao lado de `/search` e `/map`: depois dela, `/images/browse` seria lida como `image_id="browse"` e responderia 422 (RFC-032 §7). Um teste a pede e exige o 200.
- **Aceita qualquer conjunto de filtros, o vazio inclusive**: o acervo inteiro por data é uma pergunta válida, que custa a mesma varredura do mapa do Brasil inteiro. Recusá-la seria política sem motivo. A interface só a faz com um círculo (§6.1), e a galeria sem filtros fica fora (§11).

### 5.2 A ordem e o cursor

```sql
SELECT <as colunas da entidade, sem o embedding>
FROM images
WHERE embedding IS NOT NULL
  AND <o dispositivo, o intervalo e o círculo, por _apply_filters()>
  AND <depois do cursor>
ORDER BY captured_at DESC NULLS LAST, relative_path, id   -- newest
      -- captured_at ASC  NULLS LAST, relative_path, id   -- oldest
LIMIT :limit + 1
```

**Uma ordem total.** Só a data muda de sentido; o caminho relativo e o id são sempre crescentes, e as fotos sem data vêm por último nos dois sentidos (§3.2). Cada parte da chave tem um motivo:

- **O caminho relativo** desempata as fotos do mesmo segundo — uma rajada, um bracketing — na ordem em que a câmera as numerou (`DJI_0042`, `DJI_0043`), e põe as fotos sem data na ordem das pastas, a que alguém espera de uma lista sem data. Dentro do mesmo segundo, a ordem é a da câmera nos dois sentidos: uma escolha.
- **O id** desempata o mesmo caminho em dois discos — uma cópia —, como o desempate da busca (RFC-025 §6). Sem ele, a ordem não seria total, e o cursor repetiria ou pularia uma das cópias.
- **A comparação dos caminhos é por ponto de código nos três repositórios.** Os dois bancos usam a localidade `C` (RFC-033a §5), em que um texto UTF-8 se ordena como o `str` do Python nos dois dublês em memória. Um teste do contrato a fixa com nomes que uma localidade linguística ordenaria de outro jeito (`B` antes de `a`).

**O cursor** é a foto `after`, lida pelo `get()` (§2.4). Com a chave dela, `(c, p, i)`, a cláusula *"depois do cursor"* é:

| a foto do cursor | `newest` | `oldest` |
| --- | --- | --- |
| com data | `captured_at < c OR (captured_at = c AND (relative_path, id) > (p, i)) OR captured_at IS NULL` | o mesmo, com `captured_at > c` |
| sem data | `captured_at IS NULL AND (relative_path, id) > (p, i)` | o mesmo |

- **A foto do cursor não precisa satisfazer os filtros.** Ela é uma posição na ordem total, e não um membro da lista; um id que existe, de outro lugar, continua a lista do ponto em que ele estaria.
- **Nenhum caminho entra**: o cliente manda o id que recebeu, e o caminho da comparação é lido do banco (RFC-030 §5.1).
- **A primeira página não tem cláusula de cursor**, e uma lista sem filtros e sem cursor é a consulta com o `WHERE embedding IS NOT NULL` e mais nada.

### 5.3 O resumo da lista

Na primeira página, e só nela, uma consulta a mais:

```sql
SELECT count(*),
       count(*) FILTER (WHERE captured_at IS NULL),
       min(captured_at),
       max(captured_at)
FROM images
WHERE embedding IS NOT NULL AND <os mesmos filtros>
```

| campo do `summary` | o quê |
| --- | --- |
| `total` | Quantas fotos a lista tem, as sem data inclusive |
| `undated` | Quantas delas não têm data: o tamanho do grupo do divisor (§3.2). Com um intervalo de datas, 0 — elas nunca combinam |
| `oldest_captured_at`, `newest_captured_at` | A primeira e a última data da lista, sem fuso, como o `captured_at` (RFC-028 §5); `null` se nenhuma foto tem data |
| `excluded_unknown_date` | O da busca, pela mesma função, `count_hidden_by_unknown_date()`: `null` sem intervalo de datas, e sem consulta |
| `excluded_unknown_position` | O da busca, pela mesma função, `count_hidden_by_unknown_position()`: `null` sem círculo, e sem consulta |

- **Só na primeira página**, porque a interface o desenha uma vez, no cabeçalho da lista. As páginas seguintes não pagam de novo por um número que a tela já tem — a regra do RFC-028 §8.2 para uma consulta que ninguém pediu. Os dois `excluded_*` ficam dentro do `summary`, e não ao lado, para que `null` continue querendo dizer *"não houve esse filtro"*, e não *"esta página não contou"*.
- **O custo** é uma varredura com o `WHERE` da página, sem ordenação. A contagem das fotos sem posição, com a mesma forma, custou 26 ms no PostgreSQL 16 embarcado com 100 000 linhas (RFC-040 §14.1); com o círculo, soma-se a distância de cada linha. Entra no primeiro critério de §9.

### 5.4 O caso de uso e o repositório

**No Domain**, em `app/domain/value_objects/listing.py`: `ListingOrder` (`NEWEST`, `OLDEST`) e `ListingSummary` (`total`, `undated`, `oldest`, `newest`).

**A porta `ImageRepository` ganha dois métodos**, nos três repositórios — o PostgreSQL, o em memória e o dublê de `tests/application/fakes.py`:

- **`list_by_filters(filters, order, limit, after: Image | None) -> list[Image]`**: as fotos com embedding sob os filtros, na ordem de §5.2, depois de `after`, no máximo `limit`. No PostgreSQL, o `WHERE` é o de `_apply_filters()`, o mesmo da busca e das contagens; nos dublês, o `matches_filters()` de sempre, com o `is None` explícito que eles já têm. Cada `Image` traz a data e a posição com as fontes, como os da busca.
- **`summarize_listing(filters) -> ListingSummary`**: §5.3, sem os dois `excluded_*`, que são das funções da busca.

O contrato de `list_by_filters()`, para a docstring e para `test_listing_contract.py`: a ordem dos dois sentidos; as sem data por último nos dois; o desempate pelo caminho e pelo id; o cursor depois de uma foto datada, da última datada e de uma sem data; a lista inteira, página a página, igual à lista pedida de uma vez, sem repetição, nos dois sentidos; a parte datada de um sentido igual à do outro ao contrário; os filtros como na busca — o intervalo nunca casa uma data desconhecida, o círculo nunca casa uma posição desconhecida (RFC-020); nenhuma foto sem embedding; e uma tabela vazia dando `[]`.

**`BrowseImagesUseCase`**, num módulo novo, `browse_images.py`, ao lado de `search_images.py`: uma listagem não é uma busca semântica, e o módulo da busca diz que é sobre ela. Recebe o repositório, o `default_limit` e o `min_radius_m`, como o `FindSimilarImagesUseCase`, e:

1. confere a página e o raio com `validated_request()`, importada de `search_images.py`, antes de ler qualquer coisa — a mesma política, e não uma cópia;
2. com `after`, lê a foto pelo `get()`; sem linha, `ImageNotFoundError` (404);
3. pede `limit + 1` a `list_by_filters()`, e devolve `limit` e o `next_after`;
4. sem `after`, monta o resumo: `summarize_listing()` e as duas funções de contagem da busca, importadas como estão.

As funções da busca ficam onde estão. Movê-las para um módulo próprio, já que pertencem aos filtros e não à busca semântica, é uma limpeza que este RFC não precisa.

**O provider**, `get_browse_images_use_case()`, não depende de `get_embedding_model()`, como os do `/similar` e do `/map` (`dependencies/__init__.py`): compor o CLIP importaria o `torch`, de 13 a 18 s neste HDD (RFC-034 §9), numa rota que nunca o usa. Um teste troca o provider do modelo por um que falha se for chamado, como o `test_similar_api.py` faz.

**A resposta** passa as fotos pelo `ResolveImageLocationUseCase`, como a busca, para cada uma dizer se o disco está conectado e o caminho absoluto quando está; e as monta com `image_fields()`, a função que o `GET /images/{id}` e a busca já dividem (`image_schema.py`). O schema, `BrowseResponseSchema`, mora em `browse_schema.py`.

### 5.5 O log

Uma linha INFO por pedido: o sentido, o tamanho da página, quantas vieram, se havia cursor, quantos dispositivos e se havia intervalo ou círculo, e o tempo. **Nem as coordenadas, nem as datas do intervalo, nem o id do cursor**: a regra do RFC-026 §16.1 e do RFC-038 §4.5 para a busca, e a área que se olha é a consulta desta rota (RFC-040 §2.7).

## 6. A interface

### 6.1 A URL e o tipo `browse`

| URL | o quê |
| --- | --- |
| `/busca?todas&perto=-26.32,-48.82&raio=1000` | Todas as fotos do círculo, as mais recentes primeiro |
| `…&ordem=antigas` | As mais antigas primeiro |
| `…&dispositivo=…&de=…&ate=…&perto_de=…&foto=…` | Os filtros e o detalhe, com os nomes da busca (RFC-038 §5.1) |

- **`SearchQuery` ganha um quarto tipo**, `{ kind: "browse", order }`, em `search/params.ts`. `readQuery()` o lê depois de `q`, `parecidas` e `imagem`, nessa ordem: uma URL com `q` e `todas` é uma busca por texto, como hoje uma com `q` e `parecidas`.
- **Sem `perto`, `todas` não é uma busca**, e a URL abre a tela inicial com os outros filtros. A interface só lista um lugar (§11).
- **A chave do cache** é `browse:newest` ou `browse:oldest`, com os filtros, por `searchKeyOf()`. As páginas carregadas e o cursor não vão na URL: uma recarga, como a do *"Reconectando…"*, volta à primeira página. Declarado em §10.
- **A tradução para a API** é a `apiFilters()` de sempre, com `order` e `after` ao lado.

### 6.2 Onde aparece

Ao lado da pergunta que já existe, nunca no lugar dela. Buscar *"construção"* perto dali continua possível.

| onde | a ação | leva a |
| --- | --- | --- |
| A tela inicial com um círculo e sem texto — vindo do mapa, das coordenadas ou do endereço | *"Ver todas as fotos daqui, por data"*, abaixo da barra | `/busca?todas`, com o círculo e os filtros da tela |
| O balão de uma célula do mapa | O mesmo texto, abaixo de *"Buscar perto daqui"*, com o mesmo raio | `/busca?todas`, com o círculo e os filtros do mapa. A busca que o mapa carrega não vai: é outra pergunta |
| A busca, com um círculo ligado | O mesmo texto, ao lado da contagem | `/busca?todas`, com o círculo e os filtros, sem o texto |
| A listagem | A barra: um texto e o Enter | A busca por texto, com o mesmo círculo: a volta para o ranqueamento |

- **A lista das células do mapa não muda**: o clique numa linha abre o balão, que tem a ação.
- **O detalhe não muda**: o *"Buscar perto daqui"* dele aplica o círculo à busca atual, seja qual for o tipo (RFC-038 §5.4), e na listagem isso a recentra na foto.
- **O botão do balão não leva número**: o balão conta a célula, e a lista conta o círculo (§2.5).
- **A ação nunca fica desabilitada pelo `/health`.** Com a busca em `unavailable`, a barra fica desabilitada com o `search_detail` (RFC-038 §5.2), e a listagem funciona: é o único jeito de ver as fotos de um lugar sem o modelo.

### 6.3 A tela

A listagem é a tela `/busca`, com a barra, as pílulas e os filtros ligados de sempre. O que muda:

- **A barra** fica vazia, com *"Descreva o que procurar perto daqui…"* dentro dela.
- **O cabeçalho**, onde a busca mostra *"14 resultados · 0,13 s"*: *"6.102 fotos daqui, de 03/01/2015 a 02/10/2026 · 305 sem data"*, do `summary`. Com uma foto, *"1 foto daqui"*; sem nenhuma datada, sem o intervalo; sem nenhuma sem data, sem a última parte. As datas formatadas do texto, sem `Date` (RFC-038 §2.5).
- **O sentido**: *"Mais recentes primeiro"* e *"Mais antigas primeiro"*, dois botões ao lado do cabeçalho, o escolhido marcado (`aria-pressed`). Trocar é um gesto que entra no histórico, como um filtro, e começa uma lista nova.
- **Os avisos** são os da busca, acima da grade, com os números do `summary`: as fotos sem posição, sem botão; as sem data sob um intervalo, com *"Tirar o filtro de datas"* (RFC-038 §5.3).
- **O cartão** mostra, no canto em que a busca mostra o cosseno, a data: *"14/07/2018 15:32"*, formatada do texto. O `title` e o texto para o leitor de tela dizem a fonte: *"Tirada em 14/07/2018 às 15:32, pelo relógio da câmera"*, ou *"Digitalizada em…"* para `exif_digitized`. Um cartão sem data não tem nada no canto.
- **O divisor**, antes do primeiro cartão sem data, na largura da grade: *"Sem data de captura · 305 fotos"* e, embaixo, menor, *"O arquivo não diz quando foram tiradas. Ficam no fim, na ordem das pastas."*
- **A nota de rodapé**: *"Ordenado pela data de captura, como o relógio da câmera a gravou."*
- **As páginas**: a primeira com 40 (`PAGE_SIZE`, a da busca), as seguintes com o `max_search_limit` do `/capabilities`, 100. *"Mostrar mais"* aparece enquanto o `next_after` não é `null`, e **acrescenta** a página seguinte depois das que estão na tela, por um `useInfiniteQuery` do TanStack Query cujo parâmetro de página é o `next_after`. Ao lado do botão, *"140 de 6.102"*.
- **As miniaturas** são as da busca: todas pedidas logo, com o espaço reservado, um 404 como o quadro com o nome (RFC-038 §5.3). Cada gesto pede no máximo 100.
- **O detalhe** abre sobre a grade, como na busca, e a grade fica montada com as suas páginas.
- **O cache** segue a regra da busca: um gesto marca a lista como velha antes de navegar, e voltar não refaz nada (RFC-038 §14.2). Um job que termina não refaz a lista aberta: refazer uma lista de dez páginas por cursor é refazer as dez.
- **O marco** é o da busca, `reportSearch({kind: "browse", limit, results, responseMs, paintedMs})`, do gesto — o clique na ação, em *"Mostrar mais"* ou num sentido — ao quadro seguinte à última miniatura da página.

**Os estados:**

| quando | a tela mostra |
| --- | --- |
| A primeira página em andamento | Nada, ou a lista anterior esmaecida, como a busca. Nunca *"Carregando o modelo de busca"*: não há modelo |
| Uma página seguinte em andamento | As páginas de antes, e o botão desabilitado com *"Lendo…"* |
| Nenhuma foto, e nenhuma indexada (a soma do RFC-037 §7) | *"Nenhuma foto indexada ainda."* |
| Nenhuma foto no círculo | *"Nenhuma foto até 1 km daqui."* Com outros filtros, *"…com esses filtros"*, e os avisos abaixo |
| Um erro | A mensagem de §6.4 |

### 6.4 As mensagens

| status | a interface diz |
| --- | --- |
| 400 | *"A busca não pôde ser feita com esses filtros."* A interface não manda um raio abaixo do mínimo nem um intervalo invertido, e um 400 aqui é um defeito: o status vai para o console, sem o `detail`, como na busca (RFC-038 §5.7) |
| 404 | *"A lista mudou desde a última página."*, com *"Recomeçar"*, que pede a primeira página de novo. As páginas na tela ficam até lá |
| Sem resposta | O *"Reconectando…"* da casca (RFC-037 §6.3). A tela guarda as páginas que tinha |

## 7. O Figma

Nenhuma chamada. O orçamento do roadmap acabou no RFC-039 (RFC-039 §14.1), e a listagem é a grade do nó `43:401`, já construída pelo RFC-038, com a data no canto em que o número está, no estilo dele. O divisor e o seletor do sentido saem dos tokens do RFC-037.

## 8. Testes

**No backend, na suíte padrão:**

- **`test_listing_contract.py`**, nos três repositórios: o contrato de §5.4, linha por linha; e o `summarize_listing()`, com o total, as sem data, a primeira e a última data, o 0 das sem data sob um intervalo, e os zeros e `null` numa tabela vazia.
- **`BrowseImagesUseCase`**: o `limit` e o raio recusados como na busca, antes de qualquer leitura; um `after` desconhecido dando `ImageNotFoundError`; o `next_after` com `limit` de `limit + 1` e `null` com menos; o resumo só sem `after`; as contagens `None`, e o repositório não chamado, sem intervalo e sem círculo.
- **A rota**: `GET /images/browse` respondendo 200, e não 422; o provider do modelo trocado por um que falha; o JSON sem `similarity`, com o `summary` na primeira página e `null` com `after`; 422 para um `order` desconhecido e um `after` que não é UUID; 404 para um `after` sem foto; os 400 da busca; o log sem coordenadas, sem datas e sem o id; o snapshot do OpenAPI mudando só pelos acréscimos.

**No `frontend/`, no `npm test`**, no ambiente `node` do Vitest, sem DOM:

- **A URL**: `todas` e `ordem=antigas` lidos e escritos; a precedência de `q`, `parecidas` e `imagem`; `todas` sem `perto` abrindo a tela inicial.
- **A API**: a tradução dos filtros pela `apiFilters()`, com `order` e `after`.
- **As páginas**: a grade renderizada por `react-dom/server` a partir de duas páginas, com os `data-id` na ordem da primeira seguida da segunda; o *"Mostrar mais"* com `next_after` e sem ele; 40 e depois 100.
- **O cartão e o divisor**: a data formatada do texto, inclusive à meia-noite e num salto de horário de verão; o `title` por fonte; nada no canto sem data; o divisor antes do primeiro sem data, nos dois sentidos, e nenhum sem fotos sem data.
- **O cabeçalho**: com e sem intervalo, com e sem as sem data, no singular.
- **As portas**: o destino da ação na tela inicial, no balão — com os filtros do mapa, o raio da célula e sem a busca carregada — e na busca com um círculo.
- **As mensagens** de §6.4.

## 9. Critérios de pronto

Medidos na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA), num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` e `WARM_UP_MODELS=true` no ambiente da janela, numa raiz de dados própria fora do `%TEMP%` (RFC-040 §14.1), a quente.

**O banco da medição** tem o dataset do RFC-022 e as 40 fotos de DJI do RFC-032, indexados, e depois 100 000 linhas escritas pelo `measure_map.py --seed-rows` do RFC-040: 2 000 lugares com os pesos de Zipf (s = 0,85), 60 m de dispersão, datas de 2015 a 2026 e 5% sem data, num dispositivo desconectado. Pelos pesos, o lugar mais cheio tem cerca de 6 100 fotos — uma conta, e o script imprime o número de verdade. É a obra de §2.1 no extremo: umas 60 páginas de 100.

| critério | o número |
| --- | --- |
| **A ordem da grade é a da resposta, página depois de página** | Sem número. Os ids da grade, lidos do DOM, iguais à concatenação das respostas, em três páginas, nos dois sentidos |
| **No servidor, com as 100 000:** a primeira página (40) do círculo de 1 km em volta do lugar mais cheio, com o resumo; e uma página de 100 do meio da lista, pelo cursor | `TBM` ms, mediana de cinco, cada uma: o planejamento e a execução do `EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON)`, como no RFC-032 §6.1.1, e o tempo de ida e volta ao lado. O `EXPLAIN` mostra se a ordenação é o *top-N heapsort* de §2.3. Para comparar: a busca que o balão abre sobre o mesmo círculo respondeu em 350 ms na janela (RFC-040 §13) |
| **Na janela, com as fotos de DJI:** do clique em *"Ver todas as fotos daqui, por data"*, no balão da célula de ~1 km com as 10 fotos de DJI, à grade pintada | `TBM` ms, mediana de cinco, lida do marco `search`. Do Enter à grade, na busca por texto: 544 ms (RFC-038 §13) |
| ***"Mostrar mais"*, com as 100 000:** do clique às 100 novas pintadas, no círculo do lugar mais cheio | `TBM` ms, mediana de cinco. As linhas semeadas não têm miniatura, e os cartões são o quadro do 404: o número é o da lista e da grade, e não o das imagens |

**O índice, com o critério escrito antes do número.** O script mede a primeira linha também só com o dispositivo das 100 000 — sem círculo, o pior caso para a varredura, e o caso que a interface não faz —, com e sem um B-tree em `captured_at` criado e desfeito na transação, como no RFC-028 §8.1 e no RFC-032 §6.1. **O índice entra, por uma migration, só se reduzir à metade a mediana do caso com o círculo**, que é o que a interface pede. Pelo §2.3, o esperado é que não reduza.

E seis verificações que não têm número:

- **Sem modelo.** Com `MODEL_RUNTIME_DIRECTORY` numa pasta vazia, a barra desabilitada com o `search_detail`, e a listagem respondendo.
- **Com o disco na gaveta.** As 100 000 estão num dispositivo desconectado: a lista as mostra com *"HD-BENCH · desconectado"*, sem erro.
- **A lista inteira.** Pela API, do lugar mais cheio até o `next_after` `null`, nos dois sentidos: as fotos vistas somam o `total` do resumo, nenhuma se repete, a parte datada de um sentido é a do outro ao contrário, e as sem data fecham os dois.
- **As portas.** Da tela inicial com um círculo, do balão e da busca com um círculo, a listagem abre com o círculo e os filtros certos, e sem o texto.
- **O resumo e os avisos.** Com um intervalo de datas sobre as 100 000, os números da API na tela, e *"Tirar o filtro de datas"* trazendo o grupo das sem data para o fim da lista.
- **Nenhum lugar nos logs.** As coordenadas de cada círculo usado, procuradas no `shell.log` e no `api.log`: zero ocorrências.

A medição abre a janela do SolidVision no desktop, e quem a roda avisa antes, com quanto tempo leva. Se houver uma janela do app aberta, a página é medida num Edge headless, como no RFC-043 §9.2.

## 10. Riscos

| risco | situação |
| --- | --- |
| A varredura do círculo com 100 000 fotos no PostgreSQL 16, num HDD | `TBM` em §9. A distância é calculada para cada linha com posição, sem índice espacial, que o RFC-032 §6.1 mediu e não criou. Se não servir, a saída é a do RFC-032 §11 — uma coluna de célula — e não um índice em `captured_at` |
| Relógios de câmeras diferentes | A ordem é a do relógio de cada câmera, sem fuso (RFC-028 §5). Um drone e um celular com os relógios defasados em horas se intercalam errado dentro de um dia; numa obra de meses, não muda nada. O cartão mostra a data que a câmera gravou |
| Uma data de digitalização na linha do tempo | `exif_digitized` é uma data, e entra na ordem (RFC-028 §4). O `title` do cartão diz *"Digitalizada em…"* |
| Um job indexando enquanto alguém lê a lista | O cursor continua de onde parou (§2.1). Uma foto nova, mais recente que o cursor, só aparece depois de uma lista nova; o resumo é o da primeira página |
| A foto do cursor sair do índice, ou ganhar uma data | Sair dá o 404 e *"Recomeçar"* (§6.4). Ganhar uma data — uma linha nunca examinada que um scan datou (RFC-028 §6.2) — muda a posição dela, e a página seguinte pode repetir ou pular fotos. Raro, e a lista seguinte é a certa |
| Uma grade com milhares de cartões | Cada *"Mostrar mais"* acrescenta 100 nós e 100 miniaturas, e nada sai. Sem virtualização (§11). Quantas páginas uma obra real tem não foi medido |
| As páginas perdidas numa recarga | Declarado (§6.1): o *"Reconectando…"* volta à primeira página |
| O desempate pelo caminho não ser a ordem do disparo | Vale para câmeras que numeram em sequência, como as de DJI. Para outras, a ordem dentro do mesmo segundo é estável, e não a do disparo |
| O total da lista diferente do número do balão | Declarado (§2.5): o círculo cobre a célula e as vizinhas. O filtro diz o raio |
| Muitas fotos com posição e sem data | Quantas há no acervo real não foi medido; no corpus da medição, 5%, sorteadas. Estão na lista, no fim, e o divisor diz quantas |

## 11. Fora deste RFC

- Paginar a busca por texto, que continua com 40 e 100 pela similaridade (RFC-038 §10).
- Uma galeria do acervo inteiro, sem filtro, na interface. A API já responde (§5.1); a tela é outra pergunta.
- Agrupar por dia, por mês ou por visita, e saltar para uma data. O cursor de §5.2 serve de base: um *"a partir de"* seria uma data, que pode entrar numa rota, e não um caminho.
- Outra ordem — pelo nome, pela pasta. O parâmetro `order` cabe nela.
- Mudar a ordem da busca por texto, que continua pela similaridade.
- Uma caixa como filtro, para listar exatamente a célula (RFC-032 §9).
- Comparar duas fotos da obra lado a lado, o antes e o depois.
- A virtualização da grade.

## 12. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-044-fotos-por-localizacao-sem-consulta-textual.md` | Este documento |
| `backend/app/domain/value_objects/listing.py` | `ListingOrder` e `ListingSummary` (§5.4) |
| `backend/app/application/use_cases/browse_images.py` | `BrowseImagesUseCase` (§5.4) |
| `backend/app/presentation/schemas/browse_schema.py` | `BrowseResponseSchema` e o resumo (§5.1, §5.3) |
| `backend/tests/infrastructure/persistence/test_listing_contract.py` | O contrato nos três repositórios (§8) |
| `backend/tests/application/test_browse_images.py`, `backend/tests/presentation/test_browse_api.py` | §8 |
| `experiments/rfc-044-browse/measure_browse.py` (+ `.log`, fora do git) | §9 |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/domain/repositories/image_repository.py` | `list_by_filters()` e `summarize_listing()`, com o contrato na docstring |
| `backend/app/infrastructure/persistence/postgres_image_repository.py`, `in_memory_image_repository.py`, `backend/tests/application/fakes.py` | Os dois métodos |
| `backend/app/presentation/api/v1/routers/images.py` | A rota, antes de `/{image_id}` |
| `backend/app/presentation/dependencies/__init__.py` | O provider, sem modelo |
| `frontend/src/api/openapi.json`, `schema.d.ts` | Regenerados (`npm run api:types`) |
| `frontend/src/search/params.ts`, `api.ts`, `actions.ts` | O tipo `browse`, a URL, a consulta por páginas e o gesto |
| `frontend/src/search/SearchScreen.tsx`, `ResultGrid.tsx`, `similarity.ts`, `SearchControls.tsx` | O cabeçalho, o sentido, o canto do cartão como espaço de cada tipo, o divisor, a nota, o *"Mostrar mais"* que acrescenta e a ação ao lado da contagem |
| `frontend/src/home/Home.tsx` | A ação abaixo da barra, com um círculo |
| `frontend/src/map/MapParts.tsx`, `words.ts`, `MapScreen.tsx` | A ação no balão |
| `docs/rfcs/README.md` | A linha deste RFC, que ainda não tem uma, e o status |

Não mudam: as migrations, a menos que o critério de §9 peça o índice; a `/search`, a `/similar`, o upload e o `/map`; as contagens e a sua semântica; o launcher, o `job_runner` e a casca.

## 13. O que isto muda no roadmap, e o que passa adiante

Nada no calendário. Fica proposto e parado, como o [RFC-042](rfc-042-fine-tuning-do-clip.md), até quem usa o acervo confirmar as duas recomendações de §3, e depois da Etapa 4 (o RFC-041, o instalador, em 29 e 30/10 no [roadmap do frontend](ROADMAP-frontend.md)), com a qual não compete. **Não espera mais a medição de uma obra real**: a paginação não depende dela (§2.1).

**Para o benchmark de escala:** a varredura do círculo no PostgreSQL 16, com 100 000 fotos, é o segundo número dele, ao lado da busca exata do RFC-038 §14.3.

**Para quem quiser a galeria, o agrupamento por data ou o salto para uma data:** a rota e o cursor de §5 já são a base; o que falta é a tela (§11).

## 14. O que a segunda versão mudou no rascunho

O rascunho foi escrito e revisado no mesmo dia, 22/10, antes de qualquer implementação. As decisões que ele propôs e esta versão mudou ficam aqui, à vista, como os RFCs implementados registram as suas correções.

| onde | a primeira versão dizia | esta diz, e por quê |
| --- | --- | --- |
| A foto sem data | Fora da lista, sempre, e contada no padrão do `excluded_unknown_date` | No fim da lista sem filtro de datas, e fora e contada com ele (§3.2). A contagem sem caminho até as fotos era um beco, e a foto sem data, ao contrário da sem posição, é daqui (§2.2) |
| A paginação | `TBM`, e fora deste RFC *"até alguém medir quantas fotos uma obra típica gera"* | Por cursor, nesta versão (§5.2). Com o teto e o inverter, o meio de uma lista acima de 200 fotos nunca aparece, e isso é uma conta, não uma medição (§2.1) |
| As contagens | *"Reaproveitando as duas funções de contagem (...), que já são genéricas"* | A de posição serve como está, e a de data só com um intervalo: sem ele, a do repositório responde 0 sem consultar. Sem intervalo, as sem data estão na lista, e o número delas vem do resumo (§2.2, §5.3) |
| O critério de tempo | *"Mais barato que a busca por texto equivalente, por não ter inferência nem travessia de índice vetorial"* | No PostgreSQL 16 a busca não atravessa o índice (RFC-038 §14.1). A diferença é o CLIP e o cosseno de cada linha, e a listagem não lê os vetores (§2.3) |
| O risco do índice | *"Um `ORDER BY` sem esse filtro pode usar um índice para evitar um sort em disco"* | Com o `LIMIT`, a ordenação é um *top-N* na memória — uma hipótese, que o `EXPLAIN` de §9 confere. A pergunta do índice é a da varredura, e ele só ajudaria o caso sem círculo (§2.3, §9) |
| Os dois sentidos | `TBM` se a primeira versão entrega os dois | Os dois: um parâmetro da mesma consulta (§3.1) |
| Onde aparece | A tela inicial com um círculo e o balão | E a busca com um círculo, que é onde o caso de uso começa: *"construção"* não achou o prédio pronto (§6.2) |
| A rota | Candidata, `GET /images/browse` | Decidida, com os parâmetros, a resposta, os status e o lugar no arquivo (§5.1) |
| A listagem sem filtros | Fora, *"a menos que o produto peça"* | A API a responde, porque recusá-la seria política sem motivo; a interface continua sem ela (§5.1, §11) |

## 15. Validação

> **Medido em 2026-10-22, na máquina do RFC-036**: Windows 10 Home (10.0.19045), Pentium G4560, 16 GB, HDD SATA, Python 3.12.9, o PostgreSQL 16 embarcado. Num checkout de `56187fd` com a implementação na árvore de trabalho, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` e `WARM_UP_MODELS=true`, numa raiz de dados própria em `%USERPROFILE%\SolidVision-medicoes\rfc-044`, fora do `%TEMP%`, e a página num Edge headless (§16.1). **Um log** ao lado do script, fora do git: `measure_browse.log`, com três rodadas. A completa; uma segunda, do servidor e das portas, depois de corrigir o `EXPLAIN` do script e o primeiro defeito do balão; e uma terceira, do balão, depois do segundo (§16.1). Os números das rodadas de antes das correções não entram aqui.

**O banco**: o dataset do RFC-022 e as 40 fotos de DJI, 85 fotos indexadas pela API em 80 s; depois as 100 000 linhas do `measure_map.py --seed-rows`, em 30 s. O lugar mais cheio, em 0,5029, −56,0126, tem **6 076 fotos no círculo de 1 km, 296 delas sem data** — a conta de §9 dizia cerca de 6 100. São 62 páginas: a obra de §2.1 no extremo.

| critério | o número |
| --- | --- |
| **A ordem da grade é a da resposta, página depois de página** | **Passou.** No lugar mais cheio, a primeira página e dois *"Mostrar mais"*: os 240 `data-id` da grade iguais à concatenação das três respostas, nos dois sentidos |
| **No servidor, com as 100 000** | A primeira página (41 linhas) do círculo de 1 km: **48,7 ms** de execução; o resumo, **48,9 ms**; uma página de 101 do meio da lista, pelo cursor, **52,7 ms**. O planejamento abaixo de 1 ms nos três. A ordenação é o *top-N heapsort* de §2.3. Pela API, com o launcher no ar e os modelos carregados: **142 ms** a primeira página com o resumo, **79 ms** a do meio. A busca que o balão abre sobre o mesmo círculo: 350 ms (RFC-040 §13) |
| **Na janela, com as fotos de DJI** | Do clique em *"Ver todas as fotos daqui, por data"*, no balão da célula de ~1 km com as 10 fotos de DJI, à grade pintada: **409 ms** (mediana de 5; de 334 a 797), a resposta em 240 ms — com as 100 000 linhas na tabela. Do Enter à grade, na busca por texto: 544 ms (RFC-038 §13) |
| ***"Mostrar mais"*, com as 100 000** | Do clique às 100 novas pintadas: **1 120 ms** (mediana de 5; de 958 a 2 827). A resposta, **108 ms**: o resto é a grade com 100 miniaturas pedidas de uma vez, que aqui são todas o quadro do 404, e cada uma ainda é um pedido. Ao lado do botão, no fim, *"540 de 6.076"* |

**O índice, pelo critério escrito antes do número** — a execução da primeira página, mediana de cinco, sem e com um B-tree criado e desfeito na transação:

| caso | sem índice | B-tree em `captured_at` | B-tree na chave inteira (`captured_at DESC NULLS LAST, relative_path COLLATE "C", id`) |
| --- | --- | --- | --- |
| O círculo de 1 km, o que a interface pede | 48,7 ms, *top-N heapsort* | 62 ms, *top-N heapsort*: o planejador não o usa | 46 ms, pelo índice |
| Só o dispositivo das 100 000, o pior caso para a varredura | 71 ms, *top-N heapsort* | 76 ms, *top-N heapsort* | **< 1 ms**, pelo índice, que para nas 41 primeiras |

**Nenhum dos dois reduz à metade o caso do círculo, e nenhum entra.** O B-tree só em `captured_at` não serve uma ordem cujo desempate ele não tem; o da chave inteira transforma o caso sem círculo, que a interface não faz (§11), e deixa o do círculo onde estava, porque ali o custo é a distância calculada em cada linha — como o §2.3 esperava. Se a galeria sem filtros um dia vier, é esse o índice a medir de novo.

E as seis verificações sem número:

- **Sem modelo. Passou.** Com `MODEL_RUNTIME_DIRECTORY` numa pasta vazia, o `/health` disse `search: unavailable`, a barra ficou desabilitada com o `search_detail` (*"The models are not in …"*), e a listagem do círculo das fotos de DJI respondeu com as 10.
- **Com o disco na gaveta. Passou.** As 240 fotos da grade do lugar mais cheio disseram *"HD-BENCH · desconectado"*, sem erro, nos dois sentidos.
- **A lista inteira. Passou.** Pela API, as 6 076 fotos em 62 páginas em cada sentido, igual ao `total` do resumo; nenhuma repetida; as datas de um sentido são as do outro ao contrário (nenhuma data repetida nas linhas semeadas: §16.1); e as 296 sem data fecham os dois.
- **As portas. Passou.** Da tela inicial com um círculo, da busca por *"telhado"* com um círculo e do balão de um mapa que carregava *"telhado"*, a listagem abriu com `perto=-26.32,-48.82` e `raio=1000`, e sem `q`.
- **O resumo e os avisos. Passou.** Com 2018 a 2020 sobre as 100 000, *"296 fotos ficaram de fora porque não têm data de captura."* — o `excluded_unknown_date` da API — e o cabeçalho *"1.432 fotos daqui, de 02/01/2018 a 28/12/2020"*. *"Tirar o filtro de datas"* levou a *"6.076 fotos daqui, de 01/01/2015 a 31/12/2026 · 296 sem data"*, e a lista voltou a terminar nas sem data.
- **Nenhum lugar nos logs. Passou.** As coordenadas dos dois círculos usados, procuradas nos logs da pasta de trabalho — `api.log`, `launcher.log`, `job_runner.log` e os do cluster —, com 236 linhas `browse completed`: nenhuma ocorrência. O `shell.log` não existe nesta medição (§16.1).

## 16. Correções feitas durante a implementação

Implementado em 22/10, a pedido do produto, **com as duas recomendações de §3 como estão escritas** — as mais recentes primeiro, com *"Mais antigas primeiro"* a um clique; as sem data no fim, depois do divisor. A confirmação delas com quem usa o acervo real, que §13 punha antes da implementação, continua pendente: ambas são uma linha de código para trocar (o padrão do `order`, e a posição do `NULLS LAST`).

### 16.1 O que o código contrariou

| onde | o texto dizia | o que foi feito, e por quê |
| --- | --- | --- |
| §5.4, o contrato | *"A parte datada de um sentido igual à do outro ao contrário"* | **Vale para as datas, e não para as fotos de uma mesma data.** Só a data troca de sentido (§5.2): duas fotos do mesmo segundo vêm na ordem da câmera nos dois, e a lista de um sentido ao contrário as poria trocadas. O teste do contrato fixa as duas coisas: a sequência das datas de um sentido é a do outro ao contrário, e um empate tem a mesma ordem nos dois. A verificação de §9, sobre as 100 000, compara as datas — e lá nenhuma se repete, porque as datas semeadas têm microssegundos |
| §5.2, a localidade | *"Os dois bancos usam a localidade `C`"*, e por isso o `ORDER BY relative_path` ordena por ponto de código | **O `COLLATE "C"` está escrito na consulta**, no `ORDER BY` e na comparação do cursor. A ordem deixa de depender de como o cluster foi iniciado, e custa nada: nenhum índice serve essa ordenação |
| §6.3, o cache | *"Segue a regra da busca: um gesto marca a lista como velha"* | **Um gesto descarta as listas daquele sentido que nenhuma tela mostra**, e a que ele abre começa da primeira página. As páginas são uma entrada só do cache, e uma entrada marcada como velha faz o TanStack Query pedir de novo cada página que estava na tela, um cursor depois do outro: o *"refazer as dez"* que o próprio §6.3 recusa para o job que termina. Voltar a uma lista que ainda está no cache continua não refazendo nada |
| §12, os arquivos | `SearchScreen.tsx` com o cabeçalho, o sentido, o divisor e o *"Mostrar mais"* que acrescenta | **A listagem é uma tela própria, `BrowseScreen.tsx`**, que a `SearchScreen` escolhe pelo tipo da URL, e as palavras dela estão em `browse.ts`, testadas sem DOM. Os avisos âmbar foram para `Notices.tsx`, que as duas telas usam: na listagem, o aviso das fotos sem posição não tem botão, porque sem o círculo não há listagem. `Browse.tsx` foi o primeiro nome, e o Windows não distingue `Browse.tsx` de `browse.ts` |
| §6.1, a URL | *"Sem `perto`, `todas` não é uma busca"* | **E a URL do mapa nunca carrega `todas`**: o balão tem o botão da listagem, e um mapa aberto da listagem levaria o círculo dela embora (o mapa não carrega o *"Perto de"*) e deixaria um `todas` sem círculo. A tela inicial também tira `todas` e `ordem` da URL que recebe da `/busca` |
| O balão do mapa (RFC-040) | Um botão a mais, abaixo de *"Buscar perto daqui"* | **O balão mais alto achou dois defeitos latentes do RFC-040**, os dois quando ele não cabe acima da célula — como a 1280 × 800 com a lista aberta, onde o mapa tem 498 px e o balão 199. O primeiro: o balão pedia ao Leaflet que se medisse de novo **depois de toda renderização**; medir interrompe o *auto-pan*, que termina o movimento na hora, cujo tratador renderiza o balão de novo — *"Maximum update depth exceeded"*, e a tela do mapa caía. Agora ele se mede só quando o que diz muda. O segundo: um clique num botão do balão durante o *auto-pan* (0,25 s) navegava, e o fim da animação, com o mapa ainda montado, gravava o enquadramento na URL por cima da navegação, desfazendo-a — também com *"Buscar perto daqui"*. Agora o mapa para e deixa de gravar antes de a tela mudar. Os dois foram achados pela medição; nenhum teste do `npm test` os pega, porque nenhum roda o Leaflet |
| §9, a janela | *"A medição abre a janela do SolidVision"*, e o Edge headless só com a janela do app aberta | **A página foi medida num Edge headless sempre**, com um launcher da pasta de trabalho, como no RFC-043 §9.2: nada abre na tela. Os marcos são os da página, pela mesma `finishGesture()`: um substituto da ponte da janela, posto antes de a página carregar, os guarda para o script. O que não se mede assim é o `shell.log`, e a verificação dos logs procura nos da pasta de trabalho |

### 16.2 O que o texto não previa

- **O canto do cartão é uma função obrigatória da grade** (`corner`), e não um padrão: a busca passa a similaridade, a listagem a data. Um cartão sem nada no canto também não diz nada no `aria-label` além do nome e do disco.
- **As duas consultas do repositório são públicas** (`listing_statement()`, `summary_statement()`), como a `search_statement()` e a `aggregate_statement()`, para a medição fazer o `EXPLAIN` do que a rota envia.
- **O resumo diz *"em 14/07/2018"*** quando a primeira e a última data são do mesmo dia, em vez de *"de 14/07/2018 a 14/07/2018"*; e *"Nenhuma foto daqui"* com zero.
- **O marco da listagem passa pela casca**: o `describeSearch()` do `log.ts` aceitava `text`, `similar` e `image`, e escrevia `kind=?` para o resto.
