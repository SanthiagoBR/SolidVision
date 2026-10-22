# RFC-040 — Mapa

**Status:** Implementado, **com o critério sem rede simulado**: por escolha de quem acompanhou a medição, a rede da máquina ficou ligada, e só a janela do critério deixou de resolver o host dos tiles. A medição de §13, na máquina do RFC-036, viu o mapa abrir assim com as células sobre o contorno dos 27 estados, a camada pausada com a faixa e nenhum caminho de tile no `shell.log`. Com o Brasil inteiro visível, do clique em *"Mapa"* às células desenhadas em 370 ms, com o dataset e as 40 fotos de DJI, e em 1 057 ms com 100 000 fotos; um passo de zoom do 6 para o 7, com as 100 000, em 260 ms. As 100 000 custam ~0,5 s no servidor, e a primeira rodada, feita com os modelos da busca ainda carregando nos dois núcleos da máquina, mediu 2 237 ms (§14.1). A verificação visual achou dois defeitos do balão, corrigidos (§14.1); e o User-Agent dos tiles, o próprio da janela em todas as requisições ao host, foi lido do log de rede do Chromium, depois de dois defeitos do script (§14.1)
**Etapa:** 3 do [roadmap do frontend](ROADMAP-frontend.md#etapa-3--mapa-rfc-040--qua-28)
**Depende de:** RFC-032 ([§7](rfc-032-geolocalizacao-e-busca-por-proximidade.md#7-o-mapa-precisa-de-um-endpoint-próprio) a rota do mapa, a `precision_applied`, o `MAX_MAP_CELLS` e o custo com 100 000 linhas; §2.2 o deslocamento entre a aeronave e o chão; §6 o círculo e o `min_radius_m`; [§11](rfc-032-geolocalizacao-e-busca-por-proximidade.md#11-riscos-e-trabalho-futuro) a área que os tiles levam para fora, e a listagem sem texto), RFC-037 (a casca, a CSP do build, o `shell.log`, a ponte e [§14](rfc-037-fundacao-e-casca-do-frontend.md#14-o-que-passa-adiante)), RFC-038 (os filtros na URL e a tradução deles para a API, o *"perto daqui"*, o `redactUrl()`, os avisos âmbar, o padrão do marco e [§12](rfc-038-busca.md#12-o-que-passa-adiante), a lacuna da célula sem texto; §14.1, a busca sempre exata no PostgreSQL 16), RFC-039 (o `/devices` pelas mensagens de volume, o conteúdo em coluna flexível e a invalidação quando um job termina; [§12](rfc-039-dispositivos-e-indexacao.md#12-o-que-passa-adiante)), RFC-028 §8.2 (o `excluded_unknown_date`), a D4 do roadmap
**Bloqueia:** nada. O RFC-041 empacota o que este entregar, e o teste em conta limpa não passa pelo mapa (§12)
**Migration:** não
**Medição:** nenhum piloto. Os números citados vêm dos RFCs 031, 032, 036, 038 e 039, cada um com a seção; os do RFC-032 foram medidos no PostgreSQL 17 do Docker, e os do RFC-031 noutra máquina (i7-7700, SSD). A política de uso dos tiles do OpenStreetMap foi lida em 2026-10-22, e as versões do Leaflet no npm no mesmo dia: este RFC as cita sem tê-las testado. Os fatos sobre o código vêm da leitura do branch `frontend` em `471a3a1`, e cada um diz o arquivo. A medição da implementação é `experiments/rfc-040-map/measure_map.py`, rodado em 2026-10-22 na mesma máquina, com o log ao lado (§13)

> **Um RFC curto, como o roadmap pediu** ([O prazo](ROADMAP-frontend.md#o-prazo-e-o-que-ele-obriga)): as decisões, os critérios de pronto com no máximo três números `TBM`, e o que ficou de fora. A convenção de rascunho continua valendo: todo número marcado `TBM` é *a medir*, e o que não é medição diz que é escolha, conta ou hipótese.

---

## 1. Contexto

O RFC-032 entregou `GET /images/map` antes de existir uma interface: as células de uma área, com a contagem de cada uma, sobre o universo dos filtros de dispositivo e de data, sem ler o texto da busca, e o número de fotos sem posição. A rota nunca teve um cliente. O RFC-038 ligou a busca e o *"perto daqui"*, que hoje só nasce de uma foto, e o RFC-039 pôs os cartões dos dispositivos na barra lateral. Falta a tela que mostra onde as fotos estão, e o caminho dela até a busca.

Este RFC desenha essa tela: o fundo da D4, as células na precisão do zoom, os filtros da busca, o balão de uma célula e os avisos. A leitura do código achou que uma célula não tem como abrir a busca sem texto (§2.1), que o filtro de datas esconde fotos que a resposta não conta (§2.2), e que a política dos tiles e o `shell.log` pedem mudanças na casca (§2.6, §2.7). O backend ganha um campo na resposta do mapa, sem migration e sem rota nova (§4).

As Etapas 0 a 2 foram implementadas até 21/10, quatro dias úteis antes do calendário do roadmap, e o corte 1 (*"o mapa vira perto desta foto"*) não é necessário. A folga não aumenta este RFC: ela fica para o RFC-041, a etapa com mais risco.

**O que o roadmap pede, e onde está neste documento:**

| o roadmap pede | onde |
| --- | --- |
| Uma tela com `GET /images/map`: as células da área visível, na precisão que a resposta diz ter aplicado (`precision_applied`) | §5.1, §5.3. A precisão pedida é a do zoom, e não a padrão da rota (§2.5) |
| Os filtros de dispositivo e de data, os mesmos da busca | §5.5. O de datas esconde fotos que a resposta não conta (§2.2, §4.1) |
| Clicar numa célula abre uma busca *"perto daqui"* | §5.4. A célula não tem texto (§2.1), o clique abre um balão antes (§2.3), e o raio tem de cobrir a célula (§2.4) |
| O aviso de `excluded_unknown_position` | §5.5 |
| O fundo segue a D4 | §5.2, com o que a política dos tiles pede (§2.6) |
| *Critérios:* sem rede, as células sobre o contorno dos estados; com o Brasil inteiro visível, o tempo até as células | §8. A máquina das medições tem 40 fotos com posição (§2.9) |

## 2. O que o estado atual muda no roadmap

A leitura do código e da política dos tiles achou nove pontos. Nenhum muda uma das decisões D1 a D5. O §2.2 acrescenta ao backend o que o roadmap não pedia; o §2.1 e o §2.3 mudam o que o clique numa célula faz; o §2.4 muda a lista de raios da busca; o §2.6 a §2.8 mudam a casca.

### 2.1 Uma célula não tem texto, e a `/search` exige o `q`

O RFC-038 §12 deixou a lacuna para cá: o *"perto daqui"* aplica um círculo a uma busca que já tem texto ou foto, e a rota exige o `q` (`images.py`). Há dois caminhos: a célula abre a busca atual com o círculo, ou uma rota nova lista as fotos de um círculo sem ranqueamento. O RFC-032 §11 chamou a segunda de *"o primeiro clique que a UI de mapa vai querer"*.

**A célula abre a busca atual com o círculo** (§5.4), e o mapa carrega a busca atual na URL para isso (§5.1). Sem uma busca, ela abre a tela inicial com o círculo aplicado: a tela inicial já lê os filtros da URL e os mostra como pílulas (`Home.tsx`), e a busca que se fizer ali leva o círculo junto (`actions.ts`). Nada disso pede o backend. Três razões:

- **É o que o roadmap pede**, e o mapa é o primeiro corte dele. Uma rota nova na etapa que o roadmap cortaria primeiro gastaria a folga que o RFC-041 pode precisar.
- **A rota nova não é pequena.** O RFC-032 §11 a pôs em primeiro no trabalho futuro e deixou duas decisões para um RFC próprio: a ordem (a data de captura, presumivelmente, com as fotos sem data em algum lugar dela) e a paginação.
- **No PostgreSQL 16 embarcado, a busca com um círculo é exata** (RFC-038 §14.1): devolve todas as fotos do círculo até o `limit`, na ordem da consulta. Qualquer texto, ou uma das consultas de exemplo da tela inicial, lista a célula, até 100 fotos.

**Rejeitado: o `q` opcional na `/search`.** Sem texto não há ranqueamento, e a rota passaria a responder duas perguntas com a mesma forma, a mistura que o RFC-032 §7 recusou para o mapa (*"São duas perguntas e dois endpoints"*).

### 2.2 O filtro de datas do mapa esconde fotos e não diz quantas

A rota aceita `captured_from` e `captured_to`, com as regras da busca (`images.py`), e a resposta conta só as fotos sem posição (`map_schema.py`). Com um intervalo de datas, as fotos sem data somem do mapa sem aviso: a tela diria *"não há fotos daqui nessa época"* quando a verdade pode ser *"há fotos daqui sem data, e o filtro não olhou nenhuma"*, a resposta que o RFC-028 existe para impedir. A busca diz quantas, com *"Tirar o filtro de datas"* (RFC-038 §5.3), e o mapa seria a única tela com o filtro e sem a conta.

**A resposta do mapa ganha `excluded_unknown_date`**, pela contagem que a busca já usa (§4.1). É a única mudança no backend.

### 2.3 Um clique que navega roubaria o duplo clique

O Leaflet entrega os dois `click` de um duplo clique antes do `dblclick` com que ele aproxima o mapa. Uma célula que navegasse para a busca no primeiro clique tiraria do mapa o gesto de aproximar justamente sobre as células, que é onde o usuário quer chegar mais perto. E com o Brasil inteiro visível as células têm ~110 km (§2.5): o clique abriria uma busca num raio de 100 km, quando a intenção mais provável é olhar de perto.

**O clique abre um balão sobre a célula**, com a contagem, *"Buscar perto daqui"* e *"Aproximar"* (§5.4). A busca sai do botão: um clique a mais, que mostra antes quantas fotos há ali.

### 2.4 O raio sai da lista da busca, e a célula tem de caber nele

O raio do *"perto daqui"* é escolhido numa lista que começa no `min_radius_m` (`radius.ts`), e o filtro mostra o escolhido nela. Um raio fora da lista iria na requisição com outro número na tela, o que o RFC-038 §5.2 não deixa (*"o que vai na requisição é o que está na tela"*). E a célula conta as fotos de um quadrado: um círculo que não o cobrisse deixaria de fora fotos que o balão contou.

**O raio é o menor da lista que cobre a célula inteira** (§5.4). Com a lista de hoje, uma célula de ~11 km, a do zoom de um estado, pediria 25 km, um círculo de 18 vezes a área dela, porque 5 km não a cobrem. **A lista ganha 10 km**, e o círculo fica com ~2,8 vezes a área da célula nas precisões 0, 1 e 2.

E o filtro precisa de um nome para o centro. Sem `perto_de`, o `useNearName()` diz *"uma foto"* (`SearchControls.tsx`), e a nota do *"Perto de"* manda abrir uma foto. **Um círculo sem foto é o do ponto do mapa**: *"Até 10 km do ponto do mapa"*.

### 2.5 A precisão é a da tela, e não a padrão da rota

Sem `precision`, a rota agrega a três casas, ~100 m (`aggregate_positions.py`). Com o Brasil inteiro visível, no zoom 4, uma célula de ~100 m tem um centésimo de pixel: as 24 células das fotos de DJI (RFC-032 §13) viriam na resposta e não se veriam. E um acervo espalhado passaria das 2 000 células, e a rota reduziria a precisão uma casa por vez, um agrupamento por casa (RFC-032 §7), até chegar a uma que a tela podia ter pedido de saída.

**A página pede a precisão do zoom**, a maior de 0 a 3 em que uma célula tem pelo menos 8 px, **e desenha a que a resposta diz ter aplicado** (§5.3). O teto é 3: abaixo de ~100 m, a célula afirmaria uma precisão que a posição não tem, porque ela é a da aeronave, a dezenas ou centenas de metros do chão fotografado (RFC-032 §2.2).

E a caixa corta células. A rota filtra as fotos pela caixa antes de agrupar (`aggregate_statement()`, em `postgres_image_repository.py`), e uma célula atravessada pela borda conta só o lado de dentro. **A caixa pedida é a área visível com uma margem** (§5.3), para que a borda que corta fique fora da tela.

### 2.6 A política dos tiles, lida em 2026-10-22

A D4 escolheu os tiles do OpenStreetMap *"com atribuição e sem download em massa"*. A política de uso deles (`operations.osmfoundation.org/policies/tiles`), lida em 2026-10-22 e sem data de atualização na página, pede mais, e quatro pontos mudam este RFC:

- **O User-Agent nomeia o app e não imita um navegador.** O padrão do Electron é o do Chromium, com o nome do app e o do Electron no meio (`Mozilla/5.0 … Chrome/… Electron/… Safari/537.36`). **A casca manda um User-Agent próprio nas requisições ao host dos tiles, e só nelas** (§5.6).
- **Robôs que arrastam e aproximam o mapa são proibidos**, e a medição de §8 é um robô assim. **As rodadas cronometradas rodam com o fundo do OpenStreetMap desligado**, e os tiles são conferidos numa abertura só, sem mexer no mapa.
- **A atribuição fica à vista sobre o mapa**, e não atrás de um controle ou fora da tela. Ela entra e sai com a camada, e nada a cobre (§5.2).
- ***"Offline use is not permitted on tile.openstreetmap.org."*** Nada aqui guarda tiles para depois: o fundo sem rede é o contorno (§5.2), e o único cache é o HTTP do Chromium, que respeita os cabeçalhos, como a política pede.

O URL é o que a política manda, `https://tile.openstreetmap.org/{z}/{x}/{y}.png`. Ela também **recomenda** poder trocar o servidor sem atualizar o software, e isso fica fora (§10): o host está na CSP do build (§2.8), e um URL vindo da configuração pediria uma CSP servida pela API. A página lida não fala em permissão prévia para um app.

### 2.7 O `shell.log` guardaria a área que o usuário olha

O RFC-038 §2.7 viu que, num erro de rede, o `console-message` traz a URL do recurso, que a casca grava pelo `consoleLine()` (`log.ts`). O `redactUrl()` corta só o query string, e o tile leva o lugar no caminho: `/12/1492/2358.png` é um quadrado de ~9 km de lado. Sem rede, cada tile que falha seria uma linha com o lugar, e o RFC-026 §16.1 decidiu que a consulta não vai para log nenhum: a área que se olha é a consulta do mapa.

**O `redactUrl()` corta também o caminho de uma URL de outro host** (§5.6), e **a camada do OpenStreetMap pausa quando nenhum tile de uma tela carrega** (§5.2): sem rede, um arrasto não vira uma linha por tile.

A API não precisa de nada. A linha INFO do `/images/map` tem a contagem de células, as precisões e o tempo, sem a caixa (`images.py`), e a API supervisionada não grava o access log (RFC-037 §5.3).

### 2.8 A CSP e o link da atribuição

A CSP do build aceita imagens só da própria origem, de `data:` e de `blob:` (`vite.config.mts`), e o RFC-037 §14 deixou a abertura para cá. **`img-src` ganha `https://tile.openstreetmap.org`**, e nada mais: os tiles são `<img>`, e o `connect-src` fica como está. O CSS do Leaflet entra no build como arquivo, e os estilos que ele escreve nos elementos vão pelo `element.style`, como os do React, que a CSP do build não recusou (RFC-039 §13). É uma hipótese, e a medição procura recusas no `shell.log` (§8).

E a atribuição é um link para `openstreetmap.org/copyright`, que a casca bloquearia: uma navegação para fora da origem é cancelada no `will-navigate`, e o `window.open` é negado (`main.ts`). **Esse URL, e só ele, abre no navegador padrão** (§5.6).

### 2.9 A máquina das medições tem 40 fotos com posição

As 45 imagens do RFC-022 não têm GPS (RFC-038 §14.1), e a pasta de fotos reais de DJI do RFC-032 tem 40, em 24 células de ~100 m. Com o Brasil inteiro visível, são duas células de ~110 km: o segundo critério do roadmap mediria o custo fixo de abrir o mapa, e não o de um acervo. O RFC-032 §7 mediu a rota com 100 000 linhas no PostgreSQL 17 do Docker, e o banco da instalação é o 16 embarcado (ADR-008), num HDD.

**A medição tem as duas coisas** (§8): o dataset e as 40 fotos de DJI, indexados como nos RFCs 038 e 039; e depois 100 000 linhas com posição, escritas direto no banco da raiz de dados da medição, como o `map_phase()` do `planner_check.py` do RFC-032 fez.

## 3. Decisão

| decisão | resultado |
| --- | --- |
| A célula clicada | Um balão com a contagem, *"Buscar perto daqui"* e *"Aproximar"*. A busca é a que o mapa carrega na URL, com o círculo; sem uma, a tela inicial com o círculo. Nenhuma rota nova (§2.1, §2.3, §5.4) |
| O raio | O menor da lista da busca que cobre a célula; a lista ganha 10 km. O centro sem foto é *"o ponto do mapa"* (§2.4) |
| A precisão | A do zoom, a maior de 0 a 3 com células de 8 px ou mais; as células desenhadas na `precision_applied` (§2.5, §5.3) |
| Os pedidos | A área visível com um quarto de margem de cada lado; um pedido novo só quando a área sai da última caixa, a precisão muda ou um filtro muda; o cache invalidado quando um job termina (§5.3) |
| As células | Retângulos num canvas, em cinco classes fixas por ordem de grandeza, numa matiz só, com legenda, dica e uma lista para o teclado (§5.3, §5.5) |
| O fundo | O contorno dos estados, do Natural Earth, sempre, embaixo dos tiles do OpenStreetMap; um controle desliga os tiles; uma tela sem nenhum tile carregado pausa a camada (§5.2) |
| O backend | `excluded_unknown_date` na resposta do mapa, pela contagem da busca (§4.1) |
| A casca | O host dos tiles na CSP; um User-Agent próprio para ele; o `redactUrl()` sem o caminho de outros hosts; o link da atribuição no navegador; um marco por desenho (§5.6) |
| A tela | `/mapa`, com a busca, os filtros e o enquadramento na URL, carregada só quando aberta; *"Mapa"* no cabeçalho e no *"Perto de"* da busca (§5.1) |

## 4. O backend

### 4.1 O mapa diz quantas fotos sem data o filtro escondeu

- **`excluded_unknown_date`** entra na resposta do `/images/map`, ao lado do `excluded_unknown_position`: quantas fotos com embedding, sob o filtro de dispositivo, não têm data de captura. `null` sem um intervalo de datas, e sem consulta nenhuma, como na busca (RFC-028 §8.2).
- **A conta é a da busca:** `count_hidden_by_unknown_date()`, a função de `search_images.py` que os dois casos de uso da busca chamam, sobre o universo do mapa, que não tem círculo. O repositório não muda: `count_unknown_capture_date()` existe nos três, com o teste de contrato.
- **Não se restringe à área**, como o `excluded_unknown_position` (`map_schema.py`): os dois números são do acervo sob os filtros, e não da caixa, e não mudam a cada arrasto.
- **Uma foto não conta duas vezes.** Sob um intervalo, uma foto sem data e sem posição fica fora do universo da contagem de posição, cujo `WHERE` tem a cláusula de datas, e entra só na de datas. A soma dos dois é o que falta em qualquer mapa com esses filtros.
- **O custo** é um `COUNT(*)` a mais, só com um intervalo. A contagem de posição, com a mesma forma, custou 90 ms com 100 000 linhas e sem índice, no PostgreSQL 17 (RFC-032, `planner_check.log`).
- **O `openapi.json` muda nesse campo**, e o `schema.d.ts` é regenerado. A docstring de `map_schema.py` que diz que o `frontend/` está vazio sai junto.

**Rejeitado: contar só as fotos sem data dentro da área.** Seria possível, porque uma foto sem data pode ter posição, mas poria lado a lado dois números de universos diferentes, um do acervo e outro da caixa, e um deles mudaria a cada arrasto. Os dois contam como a busca conta, sobre *"a tabela inteira sob os filtros"* (RFC-032 §6.2).

Nada mais muda: a rota, os parâmetros, a redução da precisão e o `MAX_MAP_CELLS` são os do RFC-032.

## 5. A interface

### 5.1 A tela, a URL e os caminhos até ela

| URL | o quê |
| --- | --- |
| `/mapa` | O mapa, com o Brasil inteiro |
| `/mapa?centro=-26.3214,-48.8163&zoom=9` | Num enquadramento. Escrito na URL a cada fim de arrasto ou de zoom, sem entrada no histórico |
| `…&q=telhado` | Com a busca que o balão de uma célula vai rodar: `q`, `parecidas` ou `imagem`, os nomes da busca (RFC-038 §5.1) |
| `…&dispositivo=…&de=…&ate=…` | Os filtros, com os nomes da busca. `perto`, `raio`, `perto_de` e `foto` não entram: a área do mapa é a visível |

O voltar, o avançar e o *"Reconectando…"*, que recarrega a página (RFC-037 §6.3), voltam ao mesmo enquadramento. O query string inteiro sai do `shell.log` pelo `redactUrl()` (RFC-038 §5.6), o enquadramento junto.

**Os caminhos até o mapa:** *"Busca"* e *"Mapa"* no cabeçalho, ao lado do nome, o da tela aberta realçado; e, no *"Perto de"* dos filtros da busca, *"ou escolha um lugar no mapa"*, que abre o mapa com a busca e os filtros dela. A nota que hoje manda abrir uma foto (`SearchControls.tsx`) passa a citar os dois caminhos.

**A tela é carregada só quando aberta** (`React.lazy`): o Leaflet e o contorno ficam num pedaço do build à parte, e o resto da interface não cresce. O Leaflet é usado direto, numa `ref`, sem o `react-leaflet`: é uma tela só, e o RFC-037 §4.1 ficou sem biblioteca de componentes.

### 5.2 O fundo

Três camadas, de baixo para cima:

| camada | o quê |
| --- | --- |
| O contorno | As 27 unidades da federação, de `src/map/brazil-states.json`, sempre desenhadas: a terra num tom claro e as divisas num cinza (§6) |
| O OpenStreetMap | `https://tile.openstreetmap.org/{z}/{x}/{y}.png`, do zoom 3 ao 18, uma escolha, por cima do contorno. Um tile que falha deixa ver o contorno embaixo: é assim que *"sem rede"* vira *"o contorno"*, sem detectar nada |
| As células | Por cima de tudo (§5.3) |

- **O contorno** vem do Natural Earth, 1:10m, *Admin 1 – States, Provinces*, de domínio público, como o próprio projeto (CC0): filtrado para o Brasil, simplificado pelo `mapshaper` sem abrir frestas entre estados vizinhos, com as coordenadas em três casas, e versionado. O script que o gera, `frontend/scripts/brazil-states.mjs`, roda uma vez, com rede, chama o `mapshaper` por `npx` numa versão fixa, sem pô-lo nas dependências, e grava a origem e o hash; o build não baixa nada. No máximo 200 KB, uma escolha; o tamanho medido vai para a validação. **Rejeitado: a malha do IBGE**, a oficial, mais detalhada do que um fundo precisa, e com uma licença que este RFC não precisa estudar, porque o Natural Earth não pede nada.
- **O enquadramento inicial**, sem um na URL, é o Brasil inteiro: os limites do contorno. O mapa não sai do mundo (`maxBounds`), e a área visível nunca cruza ±180°, que a API recusa (`BoundingBox`, em `position.py`).
- **O controle *"Fundo do OpenStreetMap"***, no canto de cima à direita, vem ligado, como a D4 pede. Desligado, a camada sai do mapa, nenhuma requisição vai ao host dos tiles, e a atribuição sai com ela. A escolha fica no `localStorage` da janela, lido num `try`; sem ele, vale o ligado. É também a resposta ao risco que o RFC-032 §11 nomeou, a área que se olha saindo da máquina.
- **A atribuição**, no canto de baixo à direita: *"© colaboradores do OpenStreetMap"*, com o link para `https://www.openstreetmap.org/copyright`, que abre no navegador (§5.6). O prefixo do Leaflet sai (`setPrefix(false)`): a licença dele pede o aviso na distribuição, e não no mapa (§12). A legenda fica no canto de baixo à esquerda, e nada cobre a atribuição.
- **A pausa sem rede.** Quando a camada termina uma tela (o `load` do Leaflet) sem nenhum `tileload` e com algum `tileerror`, ela sai do mapa, e uma faixa discreta diz *"Sem conexão com o OpenStreetMap: o fundo é o contorno dos estados."*, com *"Tentar de novo"*. A camada volta no evento `online` da janela, no botão e na próxima abertura da tela. A regra é uma função pura, testada sem o Leaflet.

### 5.3 As células

**A precisão pedida** é a maior de 0 a 3 em que uma célula tem pelo menos 8 px na tela, uma escolha. No Web Mercator, um grau de longitude tem `256 × 2^zoom / 360` px, e a conta dá:

| zoom | precisão | a célula | na tela |
| --- | --- | --- | --- |
| 3 a 6 | 0 | ~110 km | de 6 a 46 px |
| 7 a 10 | 1 | ~11 km | de 9 a 73 px |
| 11 a 13 | 2 | ~1 km | de 15 a 58 px |
| 14 a 18 | 3 | ~100 m | de 12 a 186 px |

**A caixa pedida** é a área visível com um quarto da largura e da altura dela de margem de cada lado, cortada nas faixas da API. A margem é uma escolha (§2.5).

**Quando se pergunta à API:**

| quando | a página |
| --- | --- |
| A tela abre | Pede a caixa, na precisão do zoom |
| Um arrasto ou um zoom termina (`moveend`) | Pede de novo se a área visível saiu da última caixa pedida, ou se a precisão mudou; senão, nada. Um arrasto curto não pergunta nada |
| Um filtro muda | Pede de novo |
| Um job chega a um estado final | O cache do mapa é invalidado, junto com o que o RFC-039 §5.3 já invalida |
| A janela volta ao foco, ou o tempo passa | Nada: o que muda as células é uma indexação |

Um pedido novo cancela o anterior na página, pelo `signal`. No servidor, o anterior termina, porque uma rota `def` não é interrompida (RFC-038 §5.2). As células de antes ficam até as novas chegarem.

**O desenho.** Cada célula é um retângulo num renderer de canvas (`L.canvas`), e não de SVG: uma resposta tem até 2 000 células, e na precisão 0 pode ter mais (RFC-032 §7). O retângulo vai do centro menos meio passo ao centro mais meio passo, nos dois eixos, na `precision_applied` da resposta: o centro é a coordenada arredondada (`map_schema.py`).

**A cor.** Cinco classes fixas, por ordem de grandeza: *"1"*, *"2 a 9"*, *"10 a 99"*, *"100 a 999"* e *"1.000 ou mais"* fotos por célula. Uma matiz só, do claro ao escuro, com 75% de opacidade e um contorno de 1 px no tom mais escuro, para a célula se ler sobre os tiles e sobre o contorno. A matiz é da família do índigo do `--color-focus`, longe do azul da água e do verde e do amarelo do fundo do OpenStreetMap; os valores são uma escolha da implementação, com o tom mais claro a pelo menos 2:1 de contraste com a terra do contorno. A contagem nunca está só na cor: está na dica, no balão e na lista.

- **Rejeitado: classes por quantis.** A mesma célula mudaria de cor a cada pedido que trouxesse outras células para a conta. Com classes fixas, a cor de uma célula só muda quando ela muda.
- **Rejeitado: círculos proporcionais à contagem.** Eles se sobrepõem onde há mais fotos, e escondem o tamanho da célula, que é o que diz a precisão.

**A legenda**, no canto de baixo à esquerda: *"Fotos por célula"*, as cinco classes e o tamanho da célula, *"Células de ~11 km"*. Quando a `precision_applied` é menor que a pedida: *"Células de ~110 km, maiores que as deste zoom: a área tem fotos em lugares demais para as de ~11 km."*

**A dica.** A célula sob o ponteiro ganha um contorno de 2 px e uma dica: *"37 fotos"* e, embaixo, menor, *"célula de ~1 km"*. O cursor de clique diz que há mais. A área de clique se estende 8 px em volta de cada célula (a `tolerance` do canvas do Leaflet), para que uma célula de 6 px se acerte. A opacidade, os contornos e a folga são escolhas.

### 5.4 A célula clicada

Um clique numa célula abre um balão sobre ela:

| no balão | o quê |
| --- | --- |
| A contagem | *"37 fotos"* e *"nesta célula de ~1 km"* |
| *"Buscar perto daqui"* | Com a busca que o mapa carrega: *"Buscar "telhado azul" perto daqui"*, *"Buscar parecidas com DJI_0042.JPG perto daqui"* ou *"Buscar parecidas com a foto solta perto daqui"*. Embaixo, *"num raio de 10 km do centro da célula"* |
| *"Aproximar"* | Quando a precisão é menor que 3: enquadra a célula, e ela vira as suas células menores |

O balão fecha no `Esc`, num clique fora e quando chegam outras células. O duplo clique aproxima, como em todo mapa.

**O raio** é o menor de `radiusOptions()`, do `min_radius_m` para cima, que cobre a célula inteira: a maior das distâncias, pela haversine, do centro aos quatro cantos. A lista da busca ganha 10 km (§2.4). Perto de 26° S, a conta dá:

| precisão | a célula | do centro ao canto | o raio |
| --- | --- | --- | --- |
| 3 | ~111 × 100 m | ~75 m | 300 m, o mínimo |
| 2 | ~1,1 × 1,0 km | ~750 m | 1 km |
| 1 | ~11 × 10 km | ~7,5 km | 10 km; sem ele na lista, 25 km |
| 0 | ~111 × 100 km | ~75 km | 100 km |

O círculo tem ~2,8 vezes a área da célula nas precisões 0 a 2, e traz fotos das células vizinhas. Na 3, o mínimo do RFC-032 §6 o faz maior, pelo motivo dele: o deslocamento da aeronave.

***"Buscar perto daqui"*** é o do detalhe da foto (RFC-038 §5.4), com outro centro e sem `perto_de`: o círculo vai para a busca que o mapa carrega, com os filtros dele, e o gesto marca a busca como velha antes de navegar, como os outros (RFC-038 §14.2). Sem uma busca, vai para a tela inicial com o círculo: a barra ganha o foco, o texto de dentro dela diz *"Descreva o que procurar perto do ponto do mapa…"*, e a pílula diz *"Até 1 km do ponto do mapa"*. A busca roda no Enter ou numa consulta de exemplo, já com o círculo.

### 5.5 Os filtros, os avisos e os estados

**Os filtros** são as pílulas da busca sem a barra: *"Dispositivos"*, com a mesma lista e o mesmo `device_id` repetido, e *"Filtros"*, só com as datas, porque a área do mapa é a visível (RFC-032 §7). A tradução para a API é a da busca (`apiQuery()`, em `params.ts`), sem o `limit` e sem o círculo. Os filtros ligados aparecem como as pílulas escuras da busca. Mudar um filtro pede as células de novo e entra no histórico, como na busca. As pílulas passam a ser um componente que a busca e o mapa usam, e as regras delas não mudam.

**A busca que o mapa carrega** aparece numa linha acima dele: *"Um clique numa célula busca "telhado azul" perto dali. O mapa mostra todas as fotos com posição, e não só as dessa busca."*, com *"Esquecer a busca"*. Sem busca, a linha não aparece.

**Os estados da tela:**

| quando | a tela mostra |
| --- | --- |
| A primeira resposta, há mais de 300 ms | *"Lendo o mapa…"* sobre o fundo |
| Um pedido seguinte, há mais de 300 ms | As células de antes, e *"Lendo o mapa…"* num canto |
| Nenhuma foto indexada (a soma do RFC-037 §7) | *"Nenhuma foto indexada ainda."* |
| Nenhuma célula na área visível | *"Nenhuma foto nesta área."* Com filtros, *"…com esses filtros"* |
| `excluded_unknown_date` maior que 0 | *"N fotos não aparecem no mapa porque não têm data de captura."*, no aviso âmbar da busca, com *"Tirar o filtro de datas"* |
| `excluded_unknown_position` maior que 0 | *"N fotos não aparecem no mapa porque não têm posição."*, no mesmo aviso, sem botão: não há como incluí-las |
| Um erro | A mensagem de §5.7 |

Os dois avisos contam sobre o acervo inteiro sob os filtros, e não sobre a área (§4.1): o texto não diz *"nesta área"*. Ficam acima do mapa, como os da busca ficam acima da grade. Os 300 ms são uma escolha.

**A lista.** *"Lista"*, no canto de cima à direita do mapa, abre ao lado dele as 20 células com mais fotos da área visível, da mais cheia para a menos: a contagem, o tamanho, o centro com duas casas e *"Buscar perto daqui"*. Um clique na linha leva o mapa até a célula e abre o balão. É o caminho do teclado e do leitor de tela até uma célula, porque o canvas não dá foco a cada uma; o mapa em si anda pelas teclas do Leaflet (as setas, o + e o −). O 20 é uma escolha.

**A altura.** O mapa ocupa a altura que sobra no conteúdo, que já é uma coluna flexível (RFC-039 §14.2).

### 5.6 A casca

- **A CSP do build:** `img-src 'self' data: blob: https://tile.openstreetmap.org`. Nada mais muda nela (§2.8).
- **O User-Agent dos tiles.** `session.defaultSession.webRequest.onBeforeSendHeaders`, filtrado por `https://tile.openstreetmap.org/*`, troca o User-Agent por `SolidVision/<versão> (+https://github.com/SanthiagoBR/SolidVision)`, com a versão do `package.json`. O nome não muda entre o desenvolvimento e a instalação, porque a política o pede estável. As outras requisições da página ficam com o padrão, e o `Referer` fica como o Chromium o manda, a origem da página; a medição o registra (§8).
- **O `redactUrl()`**, em `log.ts`: uma URL `http` ou `https` de um host que não é `127.0.0.1` nem `localhost` perde também o caminho, e vira `https://tile.openstreetmap.org/…`. As da própria origem continuam perdendo só o query string (§2.7).
- **Os links de fora.** Uma função pura, em `electron/links.ts`, diz se uma URL abre no navegador, e hoje diz sim só para `https://www.openstreetmap.org/copyright`. O `will-navigate` e o `setWindowOpenHandler` a consultam: com o sim, `shell.openExternal()`, e a janela fica onde está; com o não, o bloqueio de hoje (§2.8).
- **Um marco por desenho**, `reportMap({ cells, precisionRequested, precisionApplied, responseMs, paintedMs })`, como o `reportFolders` do RFC-039 §5.9: do gesto (o clique em *"Mapa"*, o fim de um arrasto ou de um zoom, um filtro) à resposta, e ao quadro seguinte às células desenhadas, dois `requestAnimationFrame` depois de elas entrarem no mapa. Sem a caixa e sem o enquadramento. São os números de §8.
- **A ponte** ganha `reportMap`, opcional como os outros marcos.

### 5.7 As mensagens

| status | a interface diz |
| --- | --- |
| 400 | *"Não deu para ler o mapa desta área."* A página corta a caixa nas faixas e nunca a inverte, e um 400 aqui é um defeito: o status vai para o console, e dali para o `shell.log`, sem o `detail` |
| 422 | A mesma, pelo mesmo motivo: uma precisão negativa ou um `device_id` malformado seriam defeitos da página |
| Sem resposta | O *"Reconectando…"* da casca (RFC-037 §6.3). As células de antes ficam |

Um tile que falha não é uma mensagem: é a pausa de §5.2.

## 6. O Figma

Nenhuma chamada. O orçamento do roadmap acabou no RFC-039, na oitava (RFC-039 §14.1), e o mapa não está no Figma: é uma das telas que o roadmap lista em *"Falta, e a API exige"*. A tela é construída com os tokens do RFC-037, e os do mapa são novos, escolhidos sem chamada:

| token | para quê |
| --- | --- |
| `--color-map-1` a `--color-map-5` | As cinco classes, do claro ao escuro; o 5 também é o contorno das células (§5.3) |
| `--color-map-land`, `--color-map-border` | A terra e as divisas do contorno (§5.2) |

## 7. Testes

**No backend, na suíte padrão:**

- **`AggregatePositionsUseCase`:** sem intervalo de datas, `excluded_unknown_date` é `None`, e o repositório falso não registra a contagem; com um, conta as fotos com embedding e sem data sob o filtro de dispositivo, com posição ou sem; uma foto sem data e sem posição entra só nela, e não no `excluded_unknown_position`.
- **A rota:** o campo no JSON, `null` sem datas; o snapshot do OpenAPI com o campo novo, e só ele.

**No `frontend/`, no `npm test`**, no ambiente `node` do Vitest, sem DOM:

- **A precisão:** cada linha da tabela de §5.3, nunca abaixo de 0 nem acima de 3.
- **A caixa:** a margem; o corte nas faixas; nunca invertida. A área visível dentro da última caixa, na mesma precisão e com os mesmos filtros, não pede; fora dela, com outra precisão ou com outro filtro, pede.
- **A célula:** os limites de meio passo, nas precisões 0 e 3; as cinco classes nas bordas (1, 9, 10, 99, 100, 999, 1 000).
- **O raio:** cada linha da tabela de §5.4, a 26° S e no equador; o `min_radius_m` acima do raio da célula; 10 km na lista.
- **A URL do mapa:** ler e escrever; `perto`, `raio`, `perto_de` e `foto` fora; a busca e os filtros mantidos; um enquadramento inválido ignorado.
- **O balão:** o destino de *"Buscar perto daqui"* com texto, com *"parecidas"*, com a foto solta e sem busca; os filtros mantidos; nenhum `perto_de`; os textos dos três tipos.
- **O filtro da busca:** *"Até 10 km do ponto do mapa"* sem `perto_de`, e o de uma foto como antes.
- **A legenda e os avisos:** o tamanho da célula, com e sem redução; os textos dos dois avisos, e nada com `null` ou zero; cada estado de §5.5.
- **A pausa dos tiles:** uma tela só com falhas pausa; um tile carregado não.
- **A lista:** as 20 da área visível com mais fotos, numa ordem estável.
- **A invalidação:** um job que termina invalida o mapa.
- **A casca:** o `redactUrl()` de um tile, de uma URL da API (como antes) e de uma `file:`; os links de fora, sim só para o URL de §5.6, e não para o mesmo host com outro caminho, nem para `http:`, nem para um host parecido; o User-Agent só no host dos tiles; o marco sem coordenadas.
- **A CSP do build** com o host dos tiles em `img-src`, e o resto igual.
- **O contorno:** as 27 unidades, dentro do teto de tamanho, com as coordenadas em três casas e dentro da caixa do Brasil.

## 8. Critérios de pronto

Medidos na máquina do RFC-036 (Windows 10 Home, Pentium G4560, 16 GB, HDD SATA), num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` e `WARM_UP_MODELS=true` no ambiente da janela, numa raiz de dados própria, a quente. A janela é dirigida pelo protocolo de depuração do Chromium, como no RFC-039 §8, no tamanho padrão de 1280 × 800.

**O banco da medição** tem o dataset do RFC-022 e as 40 fotos de DJI do RFC-032, indexados antes das rodadas. Depois das rodadas com eles, recebe 100 000 linhas com posição, escritas direto no cluster da raiz de dados da medição, como o `map_phase()` do `planner_check.py`: um dispositivo que não está conectado; um vetor só para todas as linhas, porque o mapa lê só o `embedding IS NOT NULL`; 90% com posição, em volta de 2 000 lugares sorteados dentro do contorno dos estados, com os pesos de Zipf (s = 0,85) e os 60 m de dispersão daquele script; e datas sorteadas de 2015 a 2026, 5% sem data. O polling do `/devices` continua ligado, como na vida real.

**As rodadas cronometradas rodam com *"Fundo do OpenStreetMap"* desligado** (§2.6): os números são das células, e não dos tiles. **A medição abre a janela no desktop, pede a quem a roda que desligue e religue a rede na hora indicada, e abre o navegador padrão uma vez**, pelo link da atribuição; quem a roda avisa antes, com quanto tempo leva.

| critério | o número |
| --- | --- |
| **Com a rede desligada, o mapa abre e mostra as células sobre o contorno dos estados** | Sem número. A rede desligada por quem mede, e a resolução do host dos tiles falhando, conferida pelo script; o fundo ligado: as células da resposta desenhadas, as 27 unidades embaixo, a camada pausada com a faixa de §5.2, e nenhum caminho de tile no `shell.log` |
| **Com o Brasil inteiro visível:** do clique em *"Mapa"*, na tela inicial, às células desenhadas, com o dataset e as 40 fotos de DJI | `TBM` ms, mediana de cinco, lida do marco `map` (§5.6). Cada rodada recarrega a página na tela inicial, com o cache do TanStack Query vazio |
| **O mesmo, com as 100 000 fotos** | `TBM` ms, mediana de cinco, com a resposta ao lado. O RFC-032 §7 mediu o agrupamento da coleção inteira em ~0,3 a ~1 s no servidor, no PostgreSQL 17 do Docker; aqui é o 16 embarcado, num HDD |
| **Um passo de zoom que muda a precisão,** do 6 para o 7, sobre a área com mais fotos, com as 100 000: do fim do zoom às células novas | `TBM` ms, mediana de cinco. O zoom chega como uma roda do mouse |

E seis verificações que não têm número:

- **Nenhuma área nos logs.** Depois da medição, o centro de cada enquadramento usado, com quatro casas, e o caminho de tile (`tile.openstreetmap.org/` seguido de um número), procurados no `shell.log` e no `api.log`: zero ocorrências.
- **Os tiles, uma vez, com a rede ligada.** O mapa aberto uma vez, sem mexer: os tiles aparecem; a atribuição está no canto de baixo à direita, e a legenda não a cobre; o User-Agent das requisições ao host, lido pelo protocolo de depuração, é o de §5.6, e não o do Chromium, e o `Referer` fica registrado. Um clique no link da atribuição abre o navegador padrão, e a janela fica onde estava.
- **O fundo desligado.** Com *"Fundo do OpenStreetMap"* desligado, cinco minutos de arrastos e zooms pelo script, e nenhuma requisição ao host dos tiles.
- **Da célula à busca.** Com uma busca por texto na URL do mapa, o balão de uma célula das fotos de DJI e *"Buscar perto daqui"* levam a `/busca` com o círculo, e toda foto da resposta tem a posição dentro dele; sem busca, à tela inicial com *"Até … do ponto do mapa"* e a barra com o foco.
- **Os avisos.** Com um filtro de datas sobre as 100 000, os dois avisos com os números da API, e *"Tirar o filtro de datas"* tirando o filtro.
- **Nenhuma recusa da CSP** no `shell.log`, na medição inteira (§2.8).

E uma informação para a validação: o tempo da busca que o balão abre, com as 100 000 fotos, do clique em *"Buscar perto daqui"* à grade, pelo marco `search` do RFC-038. É um primeiro número para a pergunta do RFC-038 §14.3, a varredura exata do 16 num acervo grande.

## 9. Riscos

| risco | situação |
| --- | --- |
| O OpenStreetMap bloquear o app, ou mudar a política | O contorno continua, e as células, que são o dado, não dependem dos tiles (D4). O URL está no código e na CSP (§10) |
| A área que se olha sair da máquina, para o OpenStreetMap | Nomeado pelo RFC-032 §11 e aceito pela D4. O controle desliga a camada (§5.2), e nenhum log local guarda a área (§5.6) |
| O agrupamento de 100 000 fotos no PostgreSQL 16 embarcado, num HDD | `TBM` em §8. As células de antes ficam até as novas chegarem, e *"Lendo o mapa…"* diz por quê. Se não servir, a saída é a do RFC-032 §11: um cache por área ou uma coluna de célula, e não um índice |
| Um pedido cancelado na página continuar no servidor | Uma rota `def` não é interrompida (RFC-038 §5.2), e um zoom rápido pode enfileirar dois ou três agrupamentos. A regra de §5.3 tira os de um arrasto curto |
| Uma célula cortada pela borda da caixa mostrar menos fotos | A margem tira o corte da área visível na precisão pedida (§2.5). Numa precisão reduzida pela rota, a célula é dez vezes maior, e a margem pode não bastar |
| O círculo de uma célula trazer fotos das vizinhas | Declarado (§5.4): ~2,8 vezes a área da célula, com o raio à vista no filtro |
| A busca que o balão abre trazer menos fotos do que a célula conta | É a busca do RFC-038: 40, e *"Mostrar mais"* até 100. Listar a célula inteira sem texto está fora (§10) |
| O Leaflet 1.9.4 ser de 2023, e o 2.0 estar em alfa | É a versão estável. O 2.0 muda a API (classes no lugar das fábricas), e a troca fica para quando ele sair do alfa |
| Um estilo do Leaflet recusado pela CSP | A hipótese de §2.8. A medição procura as recusas no `shell.log` |
| O evento `online` não refletir a internet | *"Tentar de novo"* religa a camada à mão (§5.2) |
| Tiles do cache HTTP aparecerem sem rede, misturados ao contorno | O cache é o do Chromium, que a política pede. Uma área já vista pode aparecer sem rede, e o resto é o contorno; enquanto um tile vier do cache, a camada não pausa, e as falhas viram linhas sem o lugar (§5.6) |
| O contorno ser grosseiro num zoom de rua | É o fundo sem rede, e as células são o dado. Um fundo de ruas offline custaria centenas de MB (D4) |
| O polling do `/devices` disputar com o mapa | A cada 10 s, cerca de 87 ms com 100 000 fotos, noutra máquina (RFC-031 §4.1). A medição deixa os dois ligados |

## 10. Fora deste RFC

- Listar as fotos de uma célula sem texto, com a ordem e a paginação a decidir (RFC-032 §11, item 1). É o primeiro passo depois do roadmap.
- Tiles offline de qualquer forma, outro provedor, tiles vetoriais, e trocar o servidor de tiles sem atualizar o app, que a política recomenda: a CSP do build nomeia o host, e um URL da configuração pediria uma CSP servida pela API.
- O link *"Reportar um erro no mapa"*, que a política também recomenda.
- O contorno dos outros países; nomes de lugar, a geocodificação e a busca por nome de lugar (RFC-032 §10).
- Uma caixa ou um polígono desenhado no mapa como filtro (RFC-032 §9).
- O círculo da busca desenhado no mapa, *"Ver no mapa"* no detalhe da foto, um marcador por foto e miniaturas no mapa.
- Abrir no último enquadramento, e *"Enquadrar as fotos"*.
- Células abaixo de ~100 m, as precisões 4 a 6 que a rota aceita (§2.5).
- O Leaflet 2.0, o `react-leaflet` e as bibliotecas de agrupamento.
- O tema escuro, outros idiomas e o token por sessão do RFC-036 §14.

## 11. Entregáveis

**Novos**

| arquivo | propósito |
| --- | --- |
| `docs/rfcs/rfc-040-mapa.md` | Este documento |
| `frontend/src/map/` | A tela, o Leaflet, as células, a legenda, o balão, a lista, os avisos, a pausa dos tiles e a URL, com os testes |
| `frontend/src/map/brazil-states.json` | O contorno das 27 unidades da federação, gerado e versionado (§5.2) |
| `frontend/scripts/brazil-states.mjs` | Gera o contorno do Natural Earth, e grava a origem e o hash |
| `frontend/electron/links.ts` | Os links que abrem no navegador, sem Electron, com teste |
| `experiments/rfc-040-map/measure_map.py` (+ `.log`, fora do git) | §8 e a validação |

**Modificados**

| arquivo | mudança |
| --- | --- |
| `backend/app/application/use_cases/aggregate_positions.py` | `excluded_unknown_date` (§4.1) |
| `backend/app/presentation/schemas/map_schema.py` | O campo, e a docstring do `frontend/` vazio |
| `backend/tests/application/test_aggregate_positions.py`, `backend/tests/presentation/test_map_api.py` | §7 |
| `frontend/src/api/openapi.json`, `schema.d.ts` | Regenerados (`npm run api:types`) |
| `frontend/package.json`, `package-lock.json` | `leaflet` 1.9.4 e, nas de desenvolvimento, `@types/leaflet` 1.9.22 |
| `frontend/vite.config.mts`, `scripts/csp.test.mjs` | O host dos tiles em `img-src` |
| `frontend/src/App.tsx` | A rota `/mapa`, carregada só quando aberta |
| `frontend/src/shell/Shell.tsx`, `Shell.module.css` | *"Busca"* e *"Mapa"* no cabeçalho |
| `frontend/src/search/SearchControls.tsx`, `actions.ts`, `radius.ts` | As pílulas sem a barra e sem o *"perto de"*; o ponto do mapa; o link para o mapa; o círculo de um ponto; 10 km na lista |
| `frontend/src/home/Home.tsx` | A barra com o foco, e o texto de dentro dela, quando chega um círculo sem busca |
| `frontend/src/api/queries.ts`, `keys.ts`, `src/devices/sync.ts` | A consulta do mapa, e a invalidação dela quando um job termina |
| `frontend/src/theme/tokens.css` | Os tokens de §6 |
| `frontend/electron/main.ts`, `preload.ts`, `log.ts`, `src/shell/bridge.ts` | O User-Agent dos tiles, os links de fora, o `redactUrl()` para outros hosts e o marco |
| `docs/rfcs/README.md`, `docs/rfcs/ROADMAP-frontend.md` | O status deste RFC, quando implementado |

Não mudam: as migrations, os parâmetros do `/images/map` e as outras rotas, o launcher e o protocolo dos eventos, o `job_runner`, a busca no backend e o `docker-compose.yml`.

## 12. O que passa adiante

**Para o RFC-041 (instalador):** o mapa funciona sem rede por construção, porque o Leaflet e o contorno vão no build. O User-Agent leva a versão do `package.json`, que o pacote define. O aviso de licença do Leaflet (BSD-2-Clause) vai junto com os das outras dependências, e o Natural Earth, de domínio público, não pede nada. A política dos tiles recomenda um contato publicado, que o `README.md` pode ter.

**Para quem mexer na busca:** listar uma célula sem texto (§10) é o pedido que o mapa vai fazer primeiro. E a busca que o balão abre, com as 100 000 fotos, é um primeiro número para o RFC-038 §14.3 (§8).

**Para quem mexer no `/images/map`:** o custo do agrupamento num acervo grande, no 16 embarcado, é o número de §8. Se não servir, a saída é a do RFC-032 §11.

## 13. Validação

Os `TBM` de §8 ficaram como foram escritos, à vista do que se esperava; os números que os respondem estão aqui, e o que mudou por causa deles, em §14.

**Declarado antes da medição**, em §8 e no script:

- **"As células desenhadas"** é o quadro seguinte àquele em que o canvas as desenhou: dois `requestAnimationFrame` depois de elas entrarem no mapa, lidos do marco `map` do `shell.log` (§5.6). A resposta é a que chegou depois do gesto; células servidas do cache encerram o gesto sem marco.
- **Cada rodada do Brasil inteiro** recarrega a página na tela inicial — o cache do TanStack Query vazio, como numa janela que acabou de abrir — e clica em *"Mapa"* no cabeçalho. **O passo de zoom** recarrega a página em `/mapa?centro=…&zoom=6`, espera as células e manda uma roda do mouse (`deltaY` −100) no centro do mapa, o que dá o zoom 7.
- **As rodadas cronometradas rodam com *"Fundo do OpenStreetMap"* desligado** (§2.6), escrito no `localStorage` antes da página carregar. Os tiles foram pedidos em sete aberturas, todas paradas: as três verificações visuais, as três verificações dos tiles e a primeira tela do teste do fundo desligado, antes do clique que o desliga. Nenhum arrasto e nenhum zoom com a camada ligada.
- **"As 27 unidades embaixo"**: um ponto bem dentro de cada unidade — de uma grade de 40 × 40 sobre a caixa dela, o ponto dentro do polígono mais longe dos vértices — e o pixel dele numa captura de tela com a legenda e os controles escondidos, comparado com o token da terra; um ponto sob uma célula conta como desenhado.
- **A medição abre janelas no desktop**, a do SolidVision várias vezes, por cerca de 25 minutos, e o navegador padrão uma vez, e isso foi dito antes de cada rodada.

> **Medido em 2026-10-22, na máquina do RFC-036**: Windows 10 Home (10.0.19045), Pentium G4560, 16 GB, HDD SATA, Python 3.12.9, Electron 44.9.0. Num checkout, depois de um `npm run build`, com `DATABASE_SOURCE=embedded` e `WARM_UP_MODELS=true` no ambiente da janela, e uma raiz de dados própria num diretório temporário. O banco é o PostgreSQL 16.15 embarcado. **A rede não foi desligada**: quem acompanhou a medição escolheu simular, e o critério 1 rodou numa janela aberta com `--host-resolver-rules`, que não resolve o host dos tiles; o resto da máquina, e as outras janelas, com rede (§14.3). **Um log** ao lado do script, fora do git: `measure_map.log`, com três rodadas. A completa, com todas as verificações; uma segunda, num diretório novo (§14.1), dos tiles, das 100 000 linhas e do critério 3; e uma terceira, no mesmo banco da segunda, dos tiles pelo log de rede e das rodadas das 100 000 com os modelos da busca já carregados, que são os números do critério 3 e do 4. Antes delas, duas rodadas curtas da verificação visual acharam os dois defeitos do balão de §14.1; os números delas não entram aqui. **A espera pelos modelos** é a da medição do RFC-038: o `/health` dizendo `search: ready` antes das rodadas; a rodada completa não esperava, e o que isso custou está em §14.1.

| verificação | como | resultado |
| --- | --- | --- |
| A suíte do backend | `pytest` dentro do `embedded run` (§14.2) | **2 900 passed, 93 deselected**, em 168,1 s, contra o PostgreSQL 16 embarcado. Eram 2 895 no `471a3a1`. Os 5 testes novos: o `excluded_unknown_date` no caso de uso (4) e na rota (1). O teste de integração do mapa contra o PostgreSQL ganhou o campo e o caso com datas (§14.2). O `openapi.json` mudou só nesse campo, e o teste que o compara passou |
| As verificações do frontend | `npm test`, `npm run lint`, `npm run typecheck`, `npm run format:check` | **321 testes em 32 arquivos** (eram 229 em 21): a precisão, a caixa, as células, o raio, a URL, as palavras, o balão, a pausa dos tiles e o marco do mapa (68); o contorno (4); os links de fora (3); o User-Agent dos tiles (5); o `redactUrl()` dos outros hosts e o marco na casca (7); o raio de 10 km e o ponto do mapa no filtro (3); e a invalidação do mapa (2). O ESLint, com as regras do React Compiler, o Prettier e os dois `tsc` limpos |
| Estilo e tipos do backend | `black --check`, `ruff check`, `mypy` | **Limpos.** O `mypy` com a configuração do projeto, 341 arquivos; o script da medição, à parte, também passa no `ruff`, no `black` e no `mypy --strict` |
| **Critério 1:** com a rede desligada, o mapa abre e mostra as células sobre o contorno dos estados | §8, **simulado** (§14.3) | **Cumprido na janela que não resolvia o host dos tiles.** Do clique em *"Mapa"* ao Brasil inteiro, com o fundo ligado: as 2 células da resposta desenhadas sobre o contorno (o marco `cells=2 precision=0->0 response=723 painted=893`, a primeira abertura daquela janela); **terra sob o ponto de dentro de cada uma das 27 unidades**, e o mar no tom dele; a faixa *"Sem conexão com o OpenStreetMap…"* acima do mapa e nenhum tile no DOM; as cargas que falharam, `net::ERR_NAME_NOT_RESOLVED`; e **nenhuma linha do `shell.log` com o host dos tiles**, com ou sem caminho (§14.1) |
| **Critério 2:** com o Brasil inteiro visível, do clique em *"Mapa"* às células, com o dataset e as 40 fotos de DJI | §8, o marco `map`, mediana de cinco | **370 ms** (354 a 395 ms); **a resposta em 357 ms** (345 a 382 ms). Duas células de ~110 km, na precisão 0, sobre as 40 fotos de DJI; as 45 do dataset não têm posição, e o aviso âmbar disse *"45 fotos não aparecem no mapa porque não têm posição."* |
| **Critério 3:** o mesmo, com as 100 000 fotos | §8, mediana de cinco, com os modelos da busca carregados | **1 057 ms** (933 a 1 214 ms); **a resposta em 1 023 ms** (877 a 1 178 ms), quase todo o tempo; 696 células de ~110 km. No servidor, o caso de uso inteiro custa 560 ms (§14.1). Com os modelos ainda carregando, logo depois de a janela abrir: 2 237 ms (1 781 a 4 995) na rodada completa, e 1 524 ms (1 337 a 1 697) na segunda. O RFC-032 §7 mediu o agrupamento da coleção inteira em ~0,3 a ~1 s no servidor, no PostgreSQL 17 do Docker |
| **Critério 4:** um passo de zoom do 6 para o 7, sobre a área com mais fotos, com as 100 000 | §8, mediana de cinco, com os modelos carregados | **260 ms** (178 a 970 ms) do fim do zoom às células novas; **a resposta em 246 ms** (165 a 955 ms); 300 células de ~11 km, na precisão 1, a pedida. Na rodada completa, 192 ms (151 a 925). Nas duas, a mais lenta foi a primeira, perto de 950 ms |
| As 100 000 linhas | §8, `measure_map.py --seed-rows`, dentro do `embedded run`, com a janela fechada | 100 000 linhas em 35,5 s, e em 21,7 s no diretório novo, num dispositivo desconectado (*"HD-BENCH"*): 89 843 com posição, em volta de 2 000 lugares dentro do contorno, e 5 016 sem data. O Brasil inteiro, na precisão 0: 696 células com 89 883 fotos, e 10 202 sem posição. A tabela, 16 MB de heap e 294 MB com o TOAST, onde ficam os vetores. O índice HNSW foi apagado antes, como o `map_phase()` do RFC-032 fez (§14.2) |
| Nenhuma área nos logs | §8 | **Nenhum** dos 11 textos dos 7 enquadramentos escritos pelo mapa — o centro e cada coordenada, com três ou quatro casas — e nenhum caminho de tile, no `shell.log` (1 713 linhas) e no `api.log` (614 linhas) |
| Os tiles, uma vez, com a rede ligada | §8, o mapa aberto e parado | **O User-Agent da janela em todas as requisições ao host dos tiles**: 15 tiles, e 30 conjuntos de cabeçalhos enviados a ele no log de rede do Chromium (`--log-net-log`), pelo HTTP/3, todos com `SolidVision/0.1.0 (+https://github.com/SanthiagoBR/SolidVision)`; a página declarou o do Chromium, `Mozilla/5.0 … SolidVision-dev/0.1.0 Chrome/152.0.7977.167 Electron/44.9.0 Safari/537.36`, que a janela troca (terceira rodada, §14.1). **O `Referer`, `http://127.0.0.1:57991/`**, a origem da página. **A atribuição**, *"© colaboradores do OpenStreetMap"*, no canto de baixo à direita, e a legenda não a cobre. **Um clique no link abriu o navegador padrão**, e a janela ficou em `/mapa` (rodada completa) |
| O fundo desligado | §8, cinco minutos de arrastos e zooms pelo script | **413 gestos, 154 pedidos do `/images/map` e nenhum pedido ao host dos tiles.** O controle desligado por um clique, depois de o mapa abrir com ele ligado |
| Da célula à busca | §8, com as fotos de DJI | **Com *"telhado"* na URL do mapa:** um clique na célula de ~1 km com as 10 fotos de DJI abriu o balão (*"10 fotos"*, *"nesta célula de ~1 km"*, *"Buscar “telhado” perto daqui"*, *"num raio de 1 km do centro da célula"*, *"Aproximar"*), e o botão levou a `/busca?q=telhado&perto=-26.32,-48.82&raio=1000`, sem `perto_de`: **10 resultados, todos com a posição dentro do círculo**. **Sem busca:** à tela inicial com *"Até 1 km do ponto do mapa"* e a barra com o foco, o texto dentro dela *"Descreva o que procurar perto do ponto do mapa…"*. E a primeira linha da lista abriu o balão da mesma célula |
| Os avisos | §8, um filtro de datas sobre as 100 000 | De 2018 a 2020: *"5.061 fotos não aparecem no mapa porque não têm data de captura."* e *"2.425 fotos não aparecem no mapa porque não têm posição."*, **os números da API**; *"Tirar o filtro de datas"* tirou o `de` e o `ate` da URL, e o aviso de datas saiu |
| Nenhuma recusa da CSP | §8, o `shell.log` inteiro | **Nenhuma**, na rodada completa: os estilos que o Leaflet escreve nos elementos passaram, como a hipótese de §2.8 dizia |
| A busca que o balão abre, com as 100 000 | Informativo, o marco `search` | **820 ms** (792 a 1 173 ms) do clique em *"Buscar perto daqui"*, na lista, à grade, com os modelos carregados; **a resposta em 350 ms** (320 a 665 ms); 40 resultados, com *"telhado"* e um círculo de 1 km sobre a célula mais cheia. Na rodada completa, 834 e 389 ms. É o primeiro número para o RFC-038 §14.3: a varredura exata do PostgreSQL 16, com o círculo, sobre 100 000 linhas |
| O contorno | §7, `scripts/brazil-states.test.mjs` | **27 unidades, 107 224 bytes, 6 015 vértices**, as coordenadas em três casas, dentro da caixa do Brasil; do Natural Earth 5.1.1, com o SHA-256 do arquivo baixado no próprio JSON |
| O build | `vite build` | **O mapa num pedaço à parte:** 271,7 KB de JavaScript (82,9 KB em gzip) — o Leaflet, o contorno e a tela — e 23,5 KB de CSS (7,9 KB). **O pedaço principal**, 430,2 KB (133,4 KB em gzip); eram 425,1 e 131,7 KB no RFC-039. Nenhum trecho do Leaflet nem do contorno nele |
| A verificação visual | Doze capturas da janela pelo protocolo de depuração | **O desenho conferido tela a tela**: o Brasil com os tiles e com o contorno, a dica, o balão, a lista, a nota do *"Perto de"*, o mapa sem rede, as 696 células, os avisos e o zoom 7. Achou os dois defeitos do balão de §14.1 |
| O Figma | §6 | **Nenhuma chamada** |

## 14. Correções feitas durante a implementação

Registradas à vista do texto original, como os RFCs 033 a 039 fizeram com as suas. Nenhuma muda uma das decisões de §3.

### 14.1 O que a medição e o código contrariaram

| onde | o texto dizia | o que foi medido ou feito |
| --- | --- | --- |
| §8, §9 | O agrupamento de 100 000 fotos no 16 embarcado, num HDD, era `TBM`; o RFC-032 mediu ~0,3 a ~1 s no 17 do Docker | **No servidor, ~0,5 s; na janela, ~1 s com os modelos da busca carregados, e até 2,2 s enquanto eles carregam.** O `EXPLAIN (ANALYZE, BUFFERS)` no banco da medição, no PostgreSQL 16.15, com o Brasil inteiro na precisão 0: o agrupamento em 478 ms — 328 ms na varredura, quase todos arredondando as duas coordenadas de 89 883 linhas como `numeric`, e 112 ms ordenando, que passou do `work_mem` de 4 MB e foi para o disco (1,7 MB) —; a contagem das sem posição em 26 ms; o caso de uso inteiro, chamado direto, em 560 ms (453 a 744). Tudo nos buffers: a tabela tem 16 MB de heap, e os vetores ficam no TOAST, que a consulta não toca. **As rodadas da rodada completa começaram assim que a janela abriu**, com os modelos da busca ainda carregando nos dois núcleos, e mediram 2 237 ms; esperando o `/health` dizer `ready`, como a medição do RFC-038 esperava, 1 057 ms. Se o número não servir, o custo está no arredondamento e na ordenação, e a coluna de célula do RFC-032 §11 tira os dois da consulta |
| §2.7 | *"Sem rede, cada tile que falha seria uma linha com o lugar"* | **Na abertura sem o host dos tiles, nenhuma linha do `shell.log` o nomeou**: as falhas de resolução dos `<img>` não chegaram ao `console-message` da janela. O `redactUrl()` de §5.6 continua valendo para a linha que chegar — os testes cobrem o tile, a API e o `file:` —, e a pausa de §5.2 para o número delas |
| §5.4 | O balão, um popup do Leaflet com o React dentro | **A verificação visual achou dois defeitos, corrigidos antes da rodada completa.** O Leaflet dá 1,3em de margem a todo `p` de um popup (`.leaflet-popup-content p`), um seletor mais forte que a classe de cada linha, e o balão saía espaçado: as regras agora têm duas classes. E o balão abria sob os controles do canto de cima, que são desenhados sobre o mapa: o popup ganhou um `autoPanPaddingTopLeft` de 56 px |
| §8 | Os tiles, com o User-Agent das requisições lido pelo protocolo de depuração | **Dois defeitos do script, e não do produto.** Na rodada completa, os tiles vieram do cache HTTP do Chromium — as capturas da verificação visual tinham aberto o mesmo enquadramento —, e um tile do cache não vai à rede: o script passou a esvaziar o cache antes. Na segunda, os 12 tiles que foram à rede também saíram sem User-Agent: o script procurava `User-Agent`, e pelo HTTP/2 e o HTTP/3 os nomes dos cabeçalhos são minúsculos (`user-agent`). **A terceira leu o que de fato saiu da máquina**, no log de rede do próprio Chromium (`--log-net-log`), numa janela aberta só para isso, e passou; a leitura do protocolo de depuração, corrigida para nomes minúsculos depois dela, ficou no script como segunda leitura, e não foi rodada de novo |
| §8 | A medição abre o navegador padrão uma vez, pelo link da atribuição | **O navegador herdou o ambiente da janela da medição**, que aponta o `LOCALAPPDATA` para o diretório de trabalho: o Edge, que não estava aberto, criou um perfil novo lá dentro, e ficou aberto nele. É um artefato da medição — a janela instalada roda com o `LOCALAPPDATA` do próprio usuário, e o navegador abre com o perfil dele —, mas uma medição que abre o navegador por um link precisa contar com isso |
| §8 | A raiz de dados da medição num diretório temporário | **Horas depois da rodada completa, as pastas vazias do cluster da medição tinham sumido** (`pg_notify`, `pg_twophase` e outras sete): algo varreu as pastas vazias do `%TEMP%`, e o cluster não subiu mais. A segunda rodada usou um diretório novo, e o diagnóstico do agrupamento foi feito nele, logo em seguida. A raiz de dados da instalação fica em `%LOCALAPPDATA%\SolidVision`, fora do `%TEMP%` |

### 14.2 O que o texto não previa

| onde | o que foi feito, e por quê |
| --- | --- |
| §5.6, §11 | **O User-Agent dos tiles ganhou um módulo próprio, `electron/tiles.ts`**, ao lado do `links.ts` de §11: o filtro do host, `tileUserAgent()` e a troca do cabeçalho, sem Electron, para que §7 o teste (*"o User-Agent só no host dos tiles"*). O `main.ts` só os entrega ao `webRequest` |
| §5.2, §7 | **O teste do contorno está em `scripts/brazil-states.test.mjs`**, ao lado do script que gera o arquivo, e não em `src/map/`: o `tsconfig` da página não tem os tipos do Node, e os outros testes de arquivos do build (a CSP e o `schema.d.ts`) já moram em `scripts/` |
| §5.2 | **O `mapshaper` é o 0.7.80**, publicado em 2026-10-07: o projeto publica quase todo dia, e o 0.7.95 saiu no dia desta implementação. Roda pelo `npm exec`, chamado pelo Node e sem shell, para que o filtro (`adm0_a3 == "BRA"`) chegue inteiro no Windows. **A simplificação guardou 15% dos vértices removíveis**: 107 224 bytes e 6 015 vértices, metade do teto de 200 KB, com 43 cruzamentos que o próprio `mapshaper` reparou. O Natural Earth é o 5.1.1, e a origem, a versão, o SHA-256 e o tamanho do que foi baixado ficam num membro `source` do próprio GeoJSON, que o Leaflet ignora; o script é `npm run map:outline` |
| §6 | **Os valores dos tokens**, escolhidos sem chamada: as cinco classes `#8580e0`, `#6760d6`, `#4b45c6` (o `--color-focus`, no meio), `#322b9c` e `#1f1966`; a terra `#e8e5da` e as divisas `#a9a5b5`. O tom mais claro, a 75% sobre a terra, dá **2,06:1**, acima dos 2:1 de §5.3. **O mar do contorno é o `--color-surface-muted`**, que já existia. E três tokens de tamanho: `--size-map-list`, `--size-map-min` e `--size-swatch`. O canvas não lê variáveis de CSS: as cores são lidas dos tokens quando o mapa é criado |
| §5.3, §5.4 | **O estado da tela vem só dos eventos do Leaflet.** As regras do React Compiler que o ESLint do projeto aplica (`set-state-in-effect` e `refs`) recusam um `setState` no corpo de um efeito e a leitura de uma `ref` durante o render. Então: o fim de um movimento define a área pedida; o pedido é essa área com os filtros da URL, calculado no render, e é assim que um filtro pede de novo; e o balão pertence à resposta em que foi aberto, de modo que outras células chegando o fecham por derivação, sem efeito |
| §5.5 | **`apiQuery()` ganhou uma metade sem o `limit`, `apiFilters()`**, que o mapa usa sem o círculo (`mapFilters()`): a tradução dos filtros para a API continua uma só |
| §5.4 | **O clique fora** é um `mousedown` fora do mapa e da lista. Dentro do mapa, quem fecha é o Leaflet, no `preclick`, que não chega depois de um arrasto: o balão fica aberto enquanto se arrasta o mapa. Ao abrir, o foco vai para o primeiro botão dele, para o teclado e o leitor de tela |
| §5.4 | **O balão não tem o × do Leaflet**, cujo rótulo é em inglês (*"Close popup"*): fecha no `Esc`, no clique fora e quando chegam outras células, como §5.4 diz. Os botões de zoom ganharam títulos em português, *"Aproximar"* e *"Afastar"* |
| §5.2, §5.5 | **A legenda fica acima da linha da atribuição**, e não encostada no canto: com a lista aberta, numa janela de 960 px, o mapa tem cerca de 340 px de largura, e as duas se tocariam. **A faixa da pausa fica acima do mapa**, e não sobre ele, para não cobrir nenhum controle |
| §5.6 | **Uma janela nova negada passou a deixar uma linha no `shell.log`**, com a URL pelo `redactUrl()`; antes, a negação era muda |
| §5.1 | **"Busca" leva à tela inicial, e "Mapa" a `/mapa`**, sem filtros: o Brasil inteiro, a primeira linha da tabela de §5.1. Com o mapa aberto, "Mapa" volta ao Brasil inteiro |
| §7 | **`test_search_api_integration.py`** compara o JSON inteiro do mapa contra o PostgreSQL, e precisou do campo novo. Ganhou o caso com datas: no PostgreSQL 16, as três fotos sem data entram no `excluded_unknown_date`, e a que também não tem posição não volta no `excluded_unknown_position` |
| §5.1 | **O pedaço principal do build cresceu 5,1 KB** (1,7 KB em gzip), de 425,1 para 430,2 KB: a URL do mapa, o marco, a caixa e as palavras novas da busca, que a busca e o cabeçalho importam. O Leaflet e o contorno estão só no pedaço do mapa |
| §8 | **O índice HNSW foi apagado antes das 100 000 linhas**, no banco da medição, como o `map_phase()` do RFC-032 apagou: 100 000 vetores iguais fariam da manutenção dele o custo da semeadura, e no PostgreSQL 16 embarcado ele nunca serve a busca (RFC-038 §14.1). As linhas entram por `unnest` de três vetores do PostgreSQL, dez mil por comando, com um vetor só: 35,5 s para as 100 000 |
| §8 | **A verificação visual virou uma verificação do script**, `look`: as capturas do Brasil com os tiles e com o contorno, da dica, do balão, da lista e da nota do *"Perto de"*, sem arrasto e sem zoom. E `--skip-browser` deixa uma rodada seguinte refazer os tiles sem abrir o navegador de novo |
| §13 | **A suíte do backend roda dentro do `embedded run`**, que sobe o cluster de desenvolvimento e o para no fim: com o cluster parado, os testes contra o PostgreSQL dão erro de conexão (*"stale postmaster.pid"*), e não chegam ao código |

### 14.3 O que ficou sem medição

- **O critério 1 com a rede desligada de verdade.** Quem acompanhou a medição escolheu simular: a janela do critério não resolvia o host dos tiles, e o resto da máquina seguiu com rede. Ficaram sem conferência, na janela, o evento `online` devolvendo a camada e *"Tentar de novo"*; a regra da pausa tem os testes de §7. `measure_map.py --only offline` faz a versão de §8: uma caixa na tela pede que a rede seja desligada, e outra, que seja religada.
- **Os tiles do cache HTTP aparecendo sem rede**, o risco de §9: o cache foi esvaziado antes da abertura sem o host dos tiles.
- **O tempo da primeira busca depois de a janela abrir**: na verificação *"Da célula à busca"*, a busca com *"telhado"* levou 19,5 s até a resposta, porque a janela tinha acabado de abrir e os modelos ainda aqueciam (RFC-036 §16). Não é um número do mapa, e não entra em §8.
- **O leitor de tela na lista**: a lista abriu o balão por um clique; nenhum leitor de tela foi usado.
- ***"Lendo o mapa…"*** não foi conferido na tela: com as 100 000 fotos, a resposta de ~2 s o mostraria, e a regra dos estados tem os testes de §7.
