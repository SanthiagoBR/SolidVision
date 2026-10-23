# RFC-043 — Busca por Endereço no Mapa

**Status:** Implementado — caminho B (Nominatim), escolhido pelo produto contra a recomendação do §3.3 (§7)
**Depende de:** RFC-032 (§9 e §11, que já recusaram geocodificação no backend), RFC-026 §16.1 (o produto não registra o texto de uma consulta, nem localmente), RFC-040 (o mapa, o campo de coordenadas, e a política de uso que os tiles já cumpriram), RFC-031 §9 (o `/capabilities`)
**Bloqueia:** nada
**Migration:** nenhuma
**Medição:** `experiments/rfc-043-address-search/measure_address_search.py` (+ `.log`): o serviço, pelo adaptador do produto; a página servida pela API, num Edge headless; e a espera do navegador na tela do mapa (§9). O primeiro `TBM` do §4 não foi medido, de propósito (§9.3)

> **Convenção de rascunho (RFC-026).** Todo número marcado `TBM` era *a medir* e devia ser escrito de volta aqui. **Os dois de B viraram número** (§9). O primeiro — a fração do acervo numa área com endereço de rua — não: medi-lo mandaria as posições das fotos a um serviço de terceiro, que é o que este RFC protege, e o produto escolheu B sem ele. As seções 1 a 5 são o rascunho como foi escrito, com a recomendação que o produto reverteu: reescrevê-las apagaria o porquê de cada salvaguarda das seções 7 e 8.

---

## 1. Contexto

Pedido do usuário, nesta sessão: uma barra de pesquisa no mapa para endereços, "ou pelo menos uma barra onde coloca as coordenadas". A segunda parte já foi entregue, fora deste RFC, por ser pequena e sem risco de arquitetura:

- `frontend/src/map/MapParts.tsx` (`CoordinatesControl`) — um campo sobre o mapa que centraliza nele, sem tocar a busca.
- `frontend/src/search/SearchControls.tsx` (`CoordinatesField`) — o mesmo texto, dentro de "Filtros → Perto de", que já existia para uma foto ou um clique no mapa e ganhou uma terceira porta.
- As duas reaproveitam `readPoint()` (RFC-038, exportado de `search/params.ts`), a mesma regra que já lê `perto=lat,lon` da URL. Sem rede, sem serviço novo, sem migration.

A primeira parte — digitar um endereço, não coordenadas — é outra categoria de problema, e este RFC existe porque **ela já foi considerada e recusada uma vez**, no RFC-032:

> *"Geocodificação de nome de lugar no backend — Exige internet e viola a premissa local-first; um gazetteer offline é outro RFC"* (RFC-032, tabela de alternativas recusadas, e §9 Não-objetivos).

O RFC-032 não fechou a porta: nomeou o preço (um RFC próprio) e seguiu. Este é esse RFC — não para implementar, mas para decidir se o preço vale a pena, agora que o mapa existe e alguém pediu.

## 2. Problema

Converter um texto como *"Rua das Palmeiras, 123, Criciúma, SC"* em `latitude, longitude` é geocodificação, e geocodificação não é uma função pura: ou um serviço de terceiro responde, ou uma base de endereços mora na máquina do usuário.

**Por que isso é mais sério que os tiles do OpenStreetMap.** O RFC-040 já aceitou uma dependência de rede opcional para o fundo do mapa, e o RFC-032 §11 registrou o preço dela: *"as coordenadas das fotos não saem da máquina, mas a área que o usuário olha sai, se o frontend usar tiles online"*. Um endereço digitado é pior nos dois eixos que o produto protege:

- **O texto, não só a área.** O RFC-026 §16.1 recusa logar até localmente o texto de uma busca, porque *"é o histórico de busca de um usuário escrito em um arquivo que sobrevive à requisição"*. Mandar esse mesmo tipo de texto — um endereço, que aqui tende a ser o nome ou a localização de uma propriedade rural — para um serviço de terceiro é a mesma informação, saindo da máquina em vez de só não sendo descartada nela.
- **Sem alternativa silenciosa.** Um tile que falha deixa o contorno dos estados (RFC-040 §2.6): a tela continua útil. Um endereço sem geocodificação não tem para onde cair, a não ser "use coordenadas" — o que esta sessão já entrega.

## 3. Caminhos, do mais barato ao mais caro

| opção | o que é | custo de implementação | o que ela custa ao usuário |
|---|---|---|---|
| **A — nada além do que já existe** | Coordenadas digitadas (entregue nesta sessão) e o clique no mapa (RFC-040) | zero | nenhum: sem rede nova, sem log novo |
| **B — Nominatim, ou outro serviço online, opcional** | Um campo "endereço" que chama uma API pública só quando há rede; sem rede, recusa e aponta para as coordenadas | baixo (uma rota, um client HTTP, um aviso de rede) | o endereço digitado sai da máquina a cada busca; política de uso do serviço (Nominatim pede ≤1 req/s, um User-Agent próprio e nenhum uso pesado sem acordo — a mesma classe de regra que o RFC-040 já cumpriu para os tiles) |
| **C — gazetteer offline** (Photon/Pelias auto-hospedado, ou uma base de CEPs/logradouros embarcada) | Um serviço a mais para o launcher (RFC-036) orquestrar, com uma base de endereços do Brasil embarcada | alto: um processo novo, uma dependência de dados de centenas de MB a alguns GB, tempo de integração | nenhum problema de rede ou de privacidade — mas conflita com o instalador leve que o RFC-041 ainda vai fechar (29–30/10) |

### 3.1 Por que não B, hoje

B é barato de implementar e é o que a maioria dos produtos faz. Mas o RFC-026 §16.1 já decidiu, para a busca semântica, que o texto de uma consulta é dado do usuário e fica com ele — *"não é uma conveniência de depuração"*, e logar isso exigiria "uma configuração com padrão desligado, defendida em seu próprio RFC". Mandar o mesmo tipo de texto para um serviço de terceiro, sem essa defesa, contradiz uma decisão já tomada neste projeto, não cria uma nova.

### 3.2 Por que não C, hoje

O RFC-032 §11 já classificou um gazetteer offline como trabalho futuro, não como um corte desta sprint. O [roadmap do frontend](ROADMAP-frontend.md) não tem vaga: a Etapa 4 (RFC-041, instalador) é a última antes de 31/10, e um serviço de geocodificação embarcado — com sua própria base de dados — é exatamente o tipo de peso que a Etapa 4 está tentando não carregar (Python *embeddable*, sem congelar, `runtime\postgres` e `runtime\models` já somam ~1 GB). Entra como candidato depois do instalador, não antes.

### 3.3 Recomendação

**Nenhuma das duas agora.** A, que já está entregue, cobre o que não depende de rede nem de uma base de dados nova. B e C ficam como `TBM` deste RFC, a decidir com uma pergunta anterior a qualquer medição técnica: *o acervo real tem fotos de propriedades com endereço de rua útil, ou é majoritariamente rural, onde coordenadas já são a unidade natural?* Essa pergunta é de produto, não de arquitetura, e não tem resposta no código.

*Revertida pelo produto em 22/10, que escolheu B sem a resposta: §7.*

## 4. Critérios de pronto, se este RFC for retomado

Nenhum tem número ainda — são o que precisaria ser medido antes de escolher B ou C, não antes de escolher A (já feito):

- `TBM`: fração de fotos do acervo real cuja posição cai numa área com endereçamento de rua (urbana) contra área rural/sem logradouro — a pergunta de §3.3, respondida com dados, não com suposição.
- `TBM`, se B for escolhido: latência e taxa de erro de um serviço de geocodificação público, medidas como o RFC-040 mediu os tiles, com o mesmo cuidado de User-Agent e cache que a política de uso exige.
- `TBM`, se C for escolhido: tamanho da base de endereços embarcada e tempo de busca nela, medidos como o RFC-035 mediu os modelos (pesos em `runtime`, verificação de integridade, tempo de carga).

## 5. Fora deste RFC

- Geocodificação reversa (coordenada → endereço) para mostrar no balão ou no detalhe de uma foto — mesma dependência de §2, outro caso de uso (RFC-032 §9 já recusou isso também).
- Autocomplete de endereço enquanto o usuário digita — que a política de uso do Nominatim também proíbe.
- Qualquer mudança no campo de coordenadas já entregue nesta sessão: ele não depende deste RFC e continua valendo com qualquer decisão aqui.

## 6. O que isto muda no roadmap

Nada no calendário. Implementado em 22/10, fora da sequência das etapas, e por isso `043`, e não `041`: o RFC-041 é o do empacotamento e do instalador (a Etapa 4 e a [D5](ROADMAP-frontend.md#decisões-a-travar-hoje)), e este documento o cita como tal no §3.2.

O que a Etapa 4 herda daqui foi escolhido para ser pouco: nenhum processo, nenhuma base de dados e nenhuma dependência nova (§8.4). Só a rede, na hora do uso, e duas chaves do `solidvision.env`, `ALLOW_ADDRESS_SEARCH` e `ADDRESS_SEARCH_URL`. O critério do instalador com a rede desligada não muda: sem rede, o campo diz que a busca não respondeu e aponta para as coordenadas (§8.5).

## 7. A decisão: B, contra a recomendação

Em 22/10, com as três opções e a recomendação do §3.3 à frente, o produto escolheu B. A pergunta do §3.3 continua sem resposta; o que mudou é que a decisão não espera mais por ela.

A objeção do §3.1 não desaparece: **o endereço digitado sai da máquina**, e esse é o preço declarado. O que este RFC faz é estreitá-lo ao que ele precisa ser:

| a objeção | o que a responde |
|---|---|
| o texto sai sem defesa | nada sai sem um gesto: o endereço vai no Enter ou no botão, nunca por tecla, num campo que diz antes da primeira tecla que é uma busca online (§8.5) |
| é o histórico de um usuário | nunca é escrito: nem no log, que guarda o tamanho; nem em disco, porque o cache é memória; nem num erro (§8.3) |
| não há alternativa silenciosa | as coordenadas continuam sem rede, nos dois campos; e coordenadas digitadas no campo de endereço são usadas ali, sem consulta |
| uma configuração com padrão desligado | `ALLOW_ADDRESS_SEARCH` desliga o campo e a rota; `ADDRESS_SEARCH_URL` troca o serviço, e um Nominatim próprio mantém os endereços na rede do usuário, o primeiro passo de C |

**O padrão é ligado**, e essa é a diferença que fica em relação ao RFC-026 §16.1, que pediria desligado. Instalado, ninguém escreve o arquivo do usuário, a razão por que o RFC-038 §2.3 ligou o `ALLOW_LOCAL_FILE_ACTIONS` na instalação: um padrão desligado seria um campo que nenhum usuário vê. Quem quiser o contrário escreve uma linha.

## 8. Como foi feito

### 8.1 A rota

`GET /api/v1/geocode?q=…` responde `{"place": {"latitude", "longitude", "label"}}`, ou `{"place": null}` quando nada combinou: um 200, como uma busca sem resultados.

| status | quando |
|---|---|
| 400 | o endereço vazio, recusado antes de qualquer envio |
| 403 | `ALLOW_ADDRESS_SEARCH` desligado, ou quem chama não está nesta máquina |
| 503 | o serviço não respondeu: sem rede, tempo esgotado, um status de erro, ou uma resposta que não se lê |

**Só loopback**, com o `require_loopback_client` do RFC-030: a API nunca repassa consultas de outra máquina ao serviço, com o User-Agent deste projeto. **E 403, não o 404 do `/reveal`.** Lá o 404 esconde a rota de quem sonda a API; aqui o `/capabilities` já diz que ela está desligada, e esconder não esconderia nada.

O `label` é a leitura do serviço (rua, bairro, cidade), e a interface o mostra ao lado do ponto: um endereço é ambíguo de um jeito que uma coordenada não é (§9.1).

O `/capabilities` ganhou o sexto campo, `address_search`, pelo critério do RFC-031 §9: a interface só desenha o campo com ele.

### 8.2 Do backend, e não da página

A consulta sai da API, e não do Chromium, como os tiles saem. Por três razões:

- o `connect-src 'self'` da CSP (RFC-040 §2.8) continua como estava;
- a regra de uma consulta por segundo é um relógio só, o do processo;
- trocar o serviço sem atualizar o software é uma linha do arquivo do usuário, e não um build.

### 8.3 O adaptador e a política de uso

`NominatimGeocoder`, atrás da porta `GeocoderPort` do Domain, com o caso de uso `GeocodeAddressUseCase` entre os dois: o endereço sem os espaços das pontas, e o vazio recusado. A [política de uso](https://operations.osmfoundation.org/policies/nominatim/), lida em 22/10:

| a política pede | aqui |
|---|---|
| no máximo uma consulta por segundo, somando a aplicação inteira | um lock e um relógio por processo: uma consulta espera completar o segundo desde o início da anterior, e uma falha também conta |
| um User-Agent que identifique a aplicação | `SolidVision/<versão> (+https://github.com/SanthiagoBR/SolidVision)`, o mesmo que a janela dá aos tiles (RFC-040 §5.6) |
| cache do lado do cliente | as últimas 256 respostas, em memória, pela vida do processo. "Nada encontrado" entra, e uma falha não; a chave ignora maiúsculas e espaços |
| poder trocar o serviço sem atualizar o software | `ADDRESS_SEARCH_URL` |
| nada de autocomplete | nada é enviado por tecla |
| a atribuição | "© OpenStreetMap" na linha do que foi encontrado, e o serviço nomeado no "Perto de" |

A consulta pede `format=jsonv2`, `limit=1`, `countrycodes=br` e `accept-language=pt-BR`. O país porque o mapa é o do Brasil, e "Rua das Palmeiras, 123" existe em uma dúzia de países.

**O endereço não é escrito em lugar nenhum.** O log diz que houve uma consulta, quanto ela esperou, quanto levou e se achou. O erro diz o status ou o tipo da falha, e nunca o URL, cuja query string *é* o endereço; e é levantado `from None`, sem a exceção que o carregaria encadeada. Na janela, o `redactUrl()` do shell já tirava a query string de todo URL que vai para o `shell.log` (RFC-038 §2.7), e `/api/v1/geocode?q=…` não é exceção.

### 8.4 `urllib`, e não `httpx`

A primeira versão usou `httpx`. Medido com `-X importtime`, em três rodadas a quente, ele somava **146 a 267 ms** à importação da API, que leva de 1,9 a 2,6 s: perto de 10% da subida que o RFC-036 mede, por uma consulta que um usuário faz de vez em quando. O `urllib.request` já está no grafo de importação da API.

Não entra dependência nova, nem nada para o instalador carregar, e sai um risco: o `httpx` escreve o URL inteiro de cada requisição no próprio logger, em INFO. O preço é uma conexão nova a cada consulta, sem keep-alive, o que para consultas espaçadas por quem as digita é o caso comum de qualquer forma.

### 8.5 A interface

- **No mapa**, o primeiro dos controles: *"Endereço (busca online)"* e *"Buscar"*. O mapa vai para o ponto no zoom das coordenadas, 14, a menos que já esteja mais perto, sem tocar a busca nem os filtros. A linha abaixo diz o que foi encontrado, com "© OpenStreetMap", ou por que o mapa não se moveu.
- **No "Perto de"**, a quarta porta, depois da foto, do mapa e das coordenadas: o campo *"endereço"*, a nota *"O endereço é enviado ao OpenStreetMap (Nominatim) quando você busca. As coordenadas não saem do computador."*, e o botão *"Buscar este endereço"*, que põe o centro do filtro.
- As frases são da interface, por status, como as da busca (RFC-038 §5.7): sem resposta do SolidVision; o serviço que não respondeu, que aponta para as coordenadas; a busca desligada depois que o campo foi desenhado; e nenhum lugar encontrado.
- Coordenadas digitadas no campo de endereço passam pelo `readPoint()` de sempre, e são usadas ali, sem consulta.
- O campo de coordenadas não mudou (§5).

## 9. Validação

Medido em 22/10, na máquina dos RFCs 037 a 040 (Windows 10 Home, Intel da família 6, modelo 158), numa rede doméstica, por `measure_address_search.py`, com o log ao lado. Os endereços são de lugares públicos de Santa Catarina, e nenhum veio do acervo.

### 9.1 O serviço

Pelo adaptador do produto, com o `urllib`, o User-Agent e a regra de uma consulta por segundo dele:

| o quê | medido |
|---|---|
| consultas | 22 em 21,7 s, 1,01 por segundo, nenhuma falha |
| latência até o último byte | mediana **641 ms**, p95 761 ms (de 243 a 765) |
| a primeira, com resolução, conexão e TLS | 726 ms |
| encontrados | 8 de 8 ruas, 6 de 6 cidades, 5 de 6 lugares rurais, e nenhum dos 2 inexistentes |
| o mesmo endereço, com outras maiúsculas e espaços | não saiu da máquina: o cache do adaptador |
| o cache do próprio serviço | um endereço novo em 746 ms; o mesmo, por um adaptador sem cache, em 45 ms |
| o User-Agent enviado | `SolidVision/0.1.0 (+https://github.com/SanthiagoBR/SolidVision)` |

O que a medição mostrou e o rascunho não sabia:

- **O serviço tem um cache perto do cliente.** Um endereço repetido voltou em 45 ms, contra 243 a 765 ms da primeira vez. Por isso toda latência daqui é de endereços pedidos pela primeira vez, e nenhum se repete no script. Uma segunda rodada dentro da vida desse cache mediria o cache.
- **A latência é bimodal**: de 243 a 382 ms, ou de 637 a 765 ms, sem relação com o tipo do endereço. A mediana cai no segundo grupo.
- **O rótulo importa.** "Rua Quinze de Novembro" voltou como "Rua XV de Novembro", e "Avenida Universitária" com três nomes de loteamento e bairro antes de "Criciúma". Sem o rótulo na tela, ninguém saberia se o ponto é o que quis. E "Estrada Geral Morro Albino" não foi encontrada: é o lugar rural de nome local que o §3.3 temia.

### 9.2 A página

A página que a API serve, num Edge headless, com um launcher e um cluster do diretório de trabalho. Antes de cada endereço, 4 s, o tempo de alguém digitá-lo:

| o quê | medido |
|---|---|
| do Enter ao mapa no endereço | 2 097, 792 e 421 ms: mediana **792 ms** |
| até a requisição sair | de 1 a 3 ms |
| a requisição | 2 078, 765 e 368 ms. O serviço, no `api.log`: 781, 640 e 297 ms |
| da resposta à URL com o centro novo | de 16 a 52 ms |
| um endereço que não existe | "Nenhum lugar encontrado…", e o mapa não se moveu |
| coordenadas no campo de endereço | o mapa foi até elas, com **0** requisições a `/api/v1/geocode` |
| "Perto de" | o endereço virou `perto=-28.6789167,-49.369265`, a 0,2 km do centro de Criciúma, em 1 036 ms do clique à URL (uma amostra) |
| os três logs do diretório de trabalho | 5 linhas `address search requested`, e nenhuma das 15 palavras digitadas |

**Quase todo o tempo é o do serviço.** Só a primeira busca, a primeira da tela do mapa, levou 1,3 s a mais que ele. A verificação `stall`, que pede `/capabilities` à API e nada ao serviço, mostra que nessa tela o navegador segura uma requisição por até 1,4 s:

| uma requisição à API, vista pelo navegador | esperando para sair | no servidor |
|---|---|---|
| na tela inicial | mediana de 1 ms | — |
| no mapa com o fundo do OpenStreetMap, ao abrir a tela | 307 ms | 7 ms |
| no mapa com o fundo, logo depois de mover | mediana de 106 ms, máximo de 1 380 ms | de 2 a 11 ms |
| no mapa sem o fundo, ao abrir a tela | 139 ms | 2 ms |
| no mapa sem o fundo, logo depois de mover | mediana de 3 ms, máximo de 236 ms | de 2 a 3 ms |

O servidor responde em milissegundos. O que segura a requisição é o navegador, ocupado com a tela do mapa, e mais com os tiles. Não é da rota nova: qualquer requisição à API, naquela tela, espera o mesmo. O RFC-040 não viu, porque cronometrou com o fundo desligado. Se a janela do Electron, com a GPU, espera o mesmo que um Edge headless, com renderização por software, não foi medido.

### 9.3 O que não foi medido

- **A fração do acervo numa área com endereço de rua**, o primeiro `TBM` do §4, de propósito. Medi-la pediria a geocodificação reversa das posições das fotos, que é mandá-las a um terceiro. O mais perto disso, sem mandar nada, é o §9.1: dos lugares rurais da região, o serviço achou 5 de 6.
- **A janela do Electron.** A página foi medida num Edge headless, porque a janela tem instância única pelo `userData` do shell, e havia uma aberta na máquina. O que só o shell faz, o `shell.log` e o corte da query string de todo URL que vai para ele, está coberto pelos testes dele (RFC-038 §2.7), e não por esta medição.
- **A soma de várias instalações.** A regra de uma consulta por segundo vale para a aplicação inteira, e o lock é por processo. Com poucos usuários, é o uso moderado que a política aceita; se o uso crescer, a resposta é o `ADDRESS_SEARCH_URL`, com um proxy com cache ou um Nominatim próprio.

Na suíte, 37 testes novos no backend: o caso de uso; o adaptador sobre um `opener` falso, com a regra do segundo num relógio falso, o cache, onze maneiras de o serviço não responder, e o endereço ausente de todo log e de todo erro; e a rota. Mais dois no `/capabilities` e quatro na interface. Nenhum sai da máquina.

## 10. O que passa adiante

- **Um marcador no ponto encontrado.** O mapa vai até ele e não o marca, como já fazia com as coordenadas.
- **O nome do lugar no filtro.** Com um endereço, o "Perto de" diz "Perto do ponto do mapa": o rótulo não vai na URL.
- **O enquadramento da resposta.** O Nominatim manda o `boundingbox` do lugar, que enquadraria uma cidade melhor que o zoom 14 de uma rua.
- **A espera do navegador na tela do mapa** (§9.2), que é do RFC-040 e de toda requisição à API naquela tela.
