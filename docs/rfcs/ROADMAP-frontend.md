# Roadmap — Frontend

Este roadmap cobre o trabalho entre o launcher ([RFC-036](rfc-036-launcher-e-supervisao-de-processos.md)) e um SolidVision que se instala e se usa sem terminal, **até 2026-10-31**. A interface parte de duas fontes: as funcionalidades que a API já entrega (RFCs 026 a 036) e o mockup do Figma (`jtztdSy3VPU9BzDWXHdJtQ`, a tela inicial em `44:458` e as três telas originais da `Page 1`).

Status segue as [convenções](README.md#convenções): todo número é `TBM` até a implementação escrevê-lo de volta.

---

## O prazo, e o que ele obriga

Hoje é 2026-10-19, uma segunda-feira. Até 2026-10-31 há **nove dias úteis inteiros** (20 a 23 e 26 a 30), mais o resto de hoje. O fim de semana de 24 e 25 e o próprio dia 31 são folga para atrasos, e não entram no plano.

Isso obriga a três coisas:

- **RFCs curtos.** Os RFCs 033 a 036 têm de 400 a 700 linhas, um piloto e medições em vários logs. Nesse formato, um RFC por etapa consumiria o prazo. A proposta é um RFC por etapa, escrito no começo dela em cerca de uma hora: as decisões, os critérios de pronto com no máximo três números `TBM`, e o que ficou de fora. A convenção continua valendo (nenhum número sem medição). O que muda é o tamanho.
- **Um corte decidido antes.** As etapas 0 a 2 são o produto. A 3 (mapa) e a 4 (instalador) têm, cada uma, uma versão reduzida já definida (ver [Cortes](#cortes)), para que um atraso troque escopo, e não o prazo.
- **Poucas chamadas ao Figma.** A conta é do plano Starter. O orçamento é de seis a oito chamadas no roadmap inteiro: as variáveis uma vez e o contexto de design de três ou quatro telas. As telas que o Figma não tem são construídas direto no código, com os mesmos tokens e componentes.

---

## Ponto de partida

### O que a API já entrega

| Funcionalidade | Rota | RFC | Onde aparece |
|---|---|---|---|
| Busca por texto, em PT ou EN, com filtro por dispositivo, data e círculo | `GET /api/v1/images/search` | 026, 027, 028, 032 | Busca |
| Detalhe de uma foto, com o disco plugado ou não | `GET /images/{id}` | 030 | Detalhe |
| Miniatura de 512 px com `ETag`, servida mesmo com o disco na gaveta | `GET /images/{id}/thumbnail` | 030 | Grade, detalhe |
| Mostrar no Explorer | `POST /images/{id}/reveal`, `GET /capabilities` | 030, 031 | Detalhe |
| Onde estão as fotos: contagem por célula | `GET /images/map` | 032 | Mapa |
| Dispositivos e volumes; registrar e renomear | `GET`/`POST /devices`, `PATCH /devices/{id}`, `GET /volumes` | 031 | Barra lateral, adicionar dispositivo |
| Pastas, um nível por vez, com estado | `GET /devices/{id}/folders?path=` | 031 | Pastas |
| Indexação: criar, acompanhar, cancelar | `POST`/`GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` | 029 | Pastas, barra lateral |
| Prontidão do banco e da busca | `GET /health`, com `search` (`loading`, `ready`, `unavailable` ou `on_demand`) | 008, 036 | Toda a interface |
| Subida, fases, falha, segunda abertura e parada | Eventos JSON do launcher, protocolo 1 | 036 §4.4, §14 | A casca |

### O que falta no backend para a interface

| Lacuna | Onde se resolve |
|---|---|
| **A busca por imagem só existe na CLI** ([RFC-035](rfc-035-modelos-offline.md)). O caso de uso já tem `execute_similar_to`, mas nenhuma rota o chama | Etapa 1 |
| **Ninguém serve a interface.** A API não tem CORS nem arquivos estáticos ([RFC-036 §14](rfc-036-launcher-e-supervisao-de-processos.md#14-o-que-passa-adiante)) | Etapa 0 (D2). Resolvido no RFC-037 |
| **O `/health` grava duas linhas INFO por chamada.** A interface vai perguntar a cada 2 s, o que dá cerca de 86 mil linhas por dia no `api.log` | Etapa 0. Resolvido no RFC-037 |
| **A porta não é uma proteção** ([RFC-036 §14](rfc-036-launcher-e-supervisao-de-processos.md#14-o-que-passa-adiante)). Uma página aberta no navegador alcança `127.0.0.1` | Etapa 0: conferir o `Host` (contra DNS rebinding). O token por sessão fica de fora. Resolvido no RFC-037, com o `Origin` junto |
| A foto em tamanho original. A miniatura tem 512 px (`thumbnail_max_edge`) e serve ao detalhe | Fora, a menos que sobre tempo na Etapa 1 |

O router `collections.py` é um placeholder vazio, e não uma funcionalidade. Apagar um dispositivo e o rescan ficaram fora do RFC-031 (§10), e continuam fora.

### O que o Figma tem, e o que falta nele

**Tem.** A tela inicial da v2 (`44:458`): a barra de busca, as pílulas *"Dispositivos · todos (4)"* e *"Filtros"*, três consultas de exemplo, a linha com o número de dispositivos, de fotos indexadas e a última indexação, e o aviso *"HD4 está registrado mas nunca foi indexado"*, com o botão *"Escolher pastas"*. Na `Page 1`, as três telas originais: o shell (cabeçalho e barra lateral com quatro cartões de dispositivo), a grade de resultados (4 × 3 miniaturas) e a tela de pastas do HD. Essas três telas são anteriores às correções do [RFC-031 §12](rfc-031-dispositivos-e-pastas-pela-api.md#12-o-que-muda-no-mockup), que continuam valendo: nenhuma porcentagem por pasta, *"1.204 indexadas"*, *"Parcial — parou em `…/IMG_2201.JPG`"*, a porcentagem do dispositivo sempre com a data do scan ao lado, e nenhum score nos cartões.

**Não encontrado.** O MCP lista só a `Page 1`. A página *"v2 — telas corrigidas"* não apareceu, e o `44:458` só foi alcançado pelo id. As outras telas da v2 (o shell, os resultados e as pastas) precisam do link do nó.

**Falta, e a API exige.** O detalhe da foto; adicionar um dispositivo; o progresso e o cancelamento da indexação; os filtros abertos (data e "perto daqui"); a busca por imagem (soltar uma foto); o mapa; a subida e os erros (os 35 s da primeira execução, a falha 78, a busca carregando).

### O que já foi medido no cliente

Um protótipo Electron com Vite, React, TanStack Query e react-router foi medido em 2026-09-22, numa numeração anterior dos RFCs de frontend. O código dele não está no repositório, mas os logs estão (`experiments/rfc-032-client-foundation/`). A janela ficou visível em 802 ms, o bundle de produção tinha 80 KB em gzip, e o primeiro `/health` do renderer levou 18 ms (p50), com o backend já de pé. O teste de deriva do esquema contra o OpenAPI vivo levou 1,6 s por HTTP. São a linha de base da Etapa 0.

---

## Decisões a travar hoje

| # | Decisão | Recomendação | Por quê |
|---|---|---|---|
| D1 | A casca | **Electron** | O Node 20 e o npm já estão nesta máquina, e nem o Rust (Tauri) nem o .NET (um host de WebView2) estão. O protótipo de setembro já mediu o Electron. O contrato do RFC-036 §14 foi escrito com o Node em mente (`windowsHide`). O tamanho não pesa: os modelos sozinhos têm 881 MB |
| D2 | Como a interface é servida | **Pela própria API, na mesma origem**: `frontend/dist` montado em `/`, com a API em `/api/v1` e o `/health` onde estão | Sem CORS e sem preflight. O Electron faz `loadURL` na URL do evento `ready`. No desenvolvimento, o Vite encaminha para a `API_PORT=8000` fixada no `.env`. A alternativa (`app://` e CORS) custa um preflight a cada `POST` e uma configuração a mais |
| D3 | Fechar a janela | **Para tudo** (fecha o stdin do launcher). Sem ícone de bandeja na v1 | O RFC-036 fez a parada devolver o job à fila sem gastar tentativa. Com um job em andamento, um diálogo basta: *"A indexação pausa e continua na próxima abertura"* |
| D4 | O fundo do mapa, sem rede | **Leaflet**. Com rede, os tiles do OpenStreetMap, com atribuição e sem download em massa. Sem rede, as células sobre o contorno dos estados, num GeoJSON embarcado | Um fundo de ruas offline custaria centenas de MB. As células, que são o dado, aparecem nos dois casos |
| D5 | O tamanho dos RFCs | **Um RFC curto por etapa**, do 037 ao 041 | [O prazo](#o-prazo-e-o-que-ele-obriga) |

---

## Etapa 0 — Fundação e casca (RFC-037) · ter 20 e qua 21

Liga a janela ao launcher e põe a tela inicial do Figma de pé com dados reais. Fecha o item *"uma janela de verdade"* que o RFC-036 deixou em aberto (§17.3).

**Status: implementada** ([RFC-037](rfc-037-fundacao-e-casca-do-frontend.md), medição em [§15](rfc-037-fundacao-e-casca-do-frontend.md#15-validação)). Os três critérios cumpridos, a quente. O que a medição achou e não esperava está no §16 dele: uma janela encoberta não pinta, e fechar a janela enquanto o `job_runner` carrega o modelo leva até 24 s, um defeito do RFC-036 que fica registrado.

**Escopo**
- `frontend/` com Vite, React e TypeScript, ESLint e Prettier, e Vitest. Tipos gerados do OpenAPI (`openapi-typescript`), com o teste de deriva do protótipo.
- Os tokens do Figma (cores, tipografia, raios e espaçamentos) e os componentes do shell. São duas ou três chamadas ao MCP.
- No backend: a interface servida pela API, com fallback para o `index.html` (D2); o `Host` conferido contra `127.0.0.1` e `localhost` (a suíte usa `testserver`); e o `/health` sem log INFO a cada chamada.
- No processo principal do Electron:
  - Lock de instância única: a segunda abertura foca a primeira janela.
  - Inicia `<python> -m app.launcher --supervised` com `windowsHide`, o stdin e o `stdout` em pipe e o `stderr` num arquivo de log da casca.
  - Uma tela de subida que mostra as fases. Na primeira execução, o `initdb` aparece como *"Preparando o banco pela primeira vez"*, porque leva 35 s a frio.
  - Cada evento do launcher tem destino:

    | Evento | O que a casca faz |
    |---|---|
    | `ready` | Abre a janela na `url` |
    | `already_running` | Abre a URL do launcher que já está de pé, sem ser dona dele: fechar a janela não o para |
    | `failed` | Mostra uma tela de erro com a linha e um botão *"Abrir a pasta de logs"* |
    | `address` | Recarrega a janela no endereço novo |
    | `child` | Repassa à interface |

  - Ao fechar a janela: fecha o stdin e espera `stopped`, com prazo.
- O shell (cabeçalho, barra lateral e conteúdo) e a tela inicial do `44:458` com dados reais:

  | Na tela | De onde vem |
  |---|---|
  | Dispositivos | `GET /devices` |
  | Fotos indexadas | A soma de `indexed_images` |
  | Última indexação | O `finished_at` mais recente de `GET /jobs?status=completed` |
  | O aviso de disco nunca indexado | Um dispositivo com `indexed_images` 0 e sem job ativo |

- O estado da busca, lido do `/health`:

  | `search` | O que a interface mostra |
  |---|---|
  | `loading` | *"Carregando o modelo de busca…"*. Leva até 17,5 s depois do `ready` neste HDD ([RFC-036 §16](rfc-036-launcher-e-supervisao-de-processos.md#16-validação)) |
  | `unavailable` | O `search_detail` |
  | `on_demand` | Nada. A primeira busca carrega o modelo |

**Critérios de pronto**
- Com o Docker desligado, `npm start` abre a janela e chega à tela inicial com números reais. Do comando à tela inicial, a quente: `TBM` (o backend sozinho levou 4,46 s). **Medido: 6,42 s**, mediana de cinco, do processo do Electron à tela inicial com os números da API; a janela visível em 767 ms. O Docker estava de pé, ocioso, e o modo embarcado não fala com ele (RFC-037 §15).
- Fechar a janela não deixa nenhum processo para trás. A conferência é pela lista de processos, com o critério do RFC-036 §16. **Medido:** nada ficou, em 27 aberturas; do `WM_CLOSE` ao `stopped`, 1,68 s.
- Abrir o app duas vezes traz a primeira janela para a frente, e não sobe um segundo backend. **Medido:** a segunda saiu em 387 ms, e a primeira voltou do minimizado, com um launcher só.

**Riscos**
- A API reinicia (RFC-036 §6.1), e a página perde a origem por alguns segundos. A casca precisa tratar `did-fail-load` com uma tela de *"Reconectando…"*, e não com uma página em branco.
- O caminho do Python muda entre o desenvolvimento (`.venv`) e a instalação. Ele fica numa função só, como o RFC-036 §5.5 fez com a linha de comando dos filhos.

---

## Etapa 1 — Busca (RFC-038) · qui 22 e sex 23

**Escopo**
- A barra de busca e a grade de resultados. Cada cartão tem a miniatura, o rótulo do dispositivo, o nome do arquivo e o estado desconectado, e nenhum score. A grade tem estados de vazio, sem resultados e modelo carregando.
- A busca não pagina: são até 100 resultados (`MAX_SEARCH_LIMIT`). *"Mostrar mais"* repete a busca com um `limit` maior.
- A pílula *"Dispositivos"*: uma seleção múltipla, enviada como `device_id` repetido.
- *"Filtros"*:
  - O intervalo de datas, com o aviso de `excluded_unknown_date`.
  - *"Perto daqui"*, com o aviso de `excluded_unknown_position`. O raio é em metros, no mínimo `min_radius_m`. O centro vem de *"perto desta foto"*, sem precisar do mapa.
- O detalhe da foto:
  - A miniatura, a data de captura e a origem dela, a posição e a origem dela, o dispositivo e o caminho.
  - *"Mostrar no Explorer"*, só com `local_file_actions` no `/capabilities` e o disco conectado. O 409 e o 410 viram mensagens: o disco não está plugado, ou o arquivo não está mais nele.
  - *"Copiar caminho"*, *"Parecidas com esta"* e *"Buscar perto daqui"*.
- No backend, a busca por imagem pela API:
  - **`GET /api/v1/images/{id}/similar`**, a partir de uma foto indexada. O RFC decide se usa o embedding guardado, que funciona com o disco desplugado e não precisa do modelo, ou se reabre o arquivo.
  - **`POST /api/v1/images/search/by-image`**, com os bytes da imagem no corpo (`image/jpeg` ou `image/png`), um limite de tamanho e um arquivo temporário na raiz de dados. Nenhuma rota passa a aceitar um caminho (RFC-030 §5.1). Como `/search` e `/map`, a rota literal precisa vir antes de `/{image_id}`.
  - Na interface, o usuário solta uma foto na barra de busca.

**Critérios de pronto**
- A interface não reordena os resultados: a ordem da grade é a da resposta, e um teste confere isso.
- Da tecla Enter à grade pintada com miniaturas, com uma consulta em português: `TBM`. No backend, a busca em PT leva 348 ms (RFC-035).
- Com 100 resultados, o tempo até a última miniatura aparecer: `TBM`. Cada revalidação custa cerca de 6,6 ms (RFC-030), e o Chromium abre seis conexões por host.

**Riscos**
- **As datas não têm fuso.** `captured_from` e `captured_to` são horários locais da câmera, e a API recusa um valor com `Z` ou offset (400). O `Date.toISOString()` do JavaScript acrescenta `Z`. A interface monta a data como `AAAA-MM-DD`, e um teste confere isso.
- A primeira busca depois de abrir o app espera o aquecimento, que levou 15,0 s neste HDD (RFC-036 §16). A interface diz isso, em vez de parecer travada.

---

## Etapa 2 — Dispositivos e indexação (RFC-039) · seg 26 e ter 27

**Escopo**
- A barra lateral, com os cartões de dispositivo do Figma. Cada cartão mostra:
  - O rótulo.
  - Se o disco está conectado. A interface pergunta ao `GET /devices` a cada poucos segundos: é polling, como decidiu o RFC-031.
  - As fotos indexadas.
  - A porcentagem, só com `last_scan_file_count` e com a data do scan ao lado (RFC-031 §4.2).
  - O job ativo.
- Adicionar um dispositivo: `GET /volumes` (os volumes sem `device_id`), um rótulo, e `POST /devices`, que é idempotente.
- Renomear um dispositivo: `PATCH /devices/{id}`.
- A tela de pastas, com as correções do RFC-031 §12:
  - Um nível por requisição, com uma trilha de navegação.
  - O estado de cada pasta: `indexing`, `queued`, `partial` com *"parou em …"*, `indexed` ou `never_indexed`.
  - *"1.204 indexadas"*, e a seta de expandir só onde há `has_children`.
  - Com o disco desplugado, a rota responde 409, e a tela diz *"Conecte o HD2 para ver as pastas"*.
- Escolher pastas e indexar: `POST /jobs` com `{device_id, scopes}`. Um 409 quer dizer que já há um job ativo naquele dispositivo.
- O progresso da indexação:
  - `discovered_files` cresce, e não há porcentagem antes de `discovery_complete`.
  - Os contadores de processadas, puladas e falhas.
  - O último arquivo processado.
- Cancelar: `POST /jobs/{id}/cancel`, que é cooperativo. A interface mostra *"cancelando…"* até o job terminar.
- O aviso *"nunca indexado"* da tela inicial leva a esta tela.
- Quando o launcher manda um `child` com `job_runner` `unavailable`, a interface mostra uma faixa: *"A indexação está indisponível"*, com a linha que veio no evento.

**Critérios de pronto**
- Pela interface, sem terminal: registrar um HD externo de verdade, escolher duas pastas, indexar, cancelar e indexar de novo.
- Desplugar o HD com a tela de pastas aberta: a interface mostra o disco desconectado em até `TBM` s, sem erro genérico.
- Uma pasta com 5 000 subpastas (947 ms e 689 KB no RFC-031 §8.3) não trava a interface.

---

## Etapa 3 — Mapa (RFC-040) · qua 28

**Escopo**
- Uma tela de mapa com `GET /images/map`: as células da área visível, na precisão que a resposta diz ter aplicado (`precision_applied`).
- Os filtros de dispositivo e de data, os mesmos da busca.
- Clicar numa célula abre uma busca *"perto daqui"*.
- O aviso de `excluded_unknown_position`.
- O fundo segue a D4.

**Critérios de pronto**
- Com a rede desligada, o mapa abre e mostra as células sobre o contorno dos estados.
- Com o Brasil inteiro visível, o tempo até as células aparecerem: `TBM`.

---

## Etapa 4 — Empacotamento e instalador (RFC-041) · qui 29 e sex 30

**Escopo**
- O Python 3.12 na distribuição *embeddable* oficial, com as dependências instaladas numa pasta, sem congelar. Congelar o `torch` com o PyInstaller é uma fonte conhecida de dias perdidos. O launcher já usa `sys.executable`.
- `runtime\postgres` (123 MB) e `runtime\models` (881 MB) dentro do pacote.
- electron-builder com NSIS, instalado por usuário em `%LOCALAPPDATA%\Programs`, sem elevação. Isso evita o perfil errado de uma instalação elevada (RFC-034 §13, RFC-036 §14).
- Sem assinatura de código. O SmartScreen vai avisar, e isso fica declarado.
- O teste numa segunda conta do Windows, como perfil limpo. O Windows 10 Home não tem o Windows Sandbox.
- A atualização do `README.md` (o "Como rodar"), do [índice dos RFCs](README.md) e da seção "Fora deste roadmap" do [roadmap sem Docker](ROADMAP-standalone.md#fora-deste-roadmap).

**Critérios de pronto**
- Numa conta sem nada instalado e com a rede desligada: instalar, abrir pelo atalho, indexar o dataset de demonstração e buscar.
- O tamanho do instalador: `TBM`.
- A primeira abertura, a frio: `TBM`. O RFC-036 mediu 35,0 s com o `initdb`.
- Desinstalar remove o programa. Os dados em `%LOCALAPPDATA%\SolidVision` ficam, e essa política fica escrita.

**Riscos**
- O antivírus. O RFC-036 §17.3 mediu um aquecimento de 49,6 s e outro de 104,9 s logo depois de uma instalação nova, e não separou a varredura de arquivos da disputa de disco.
- O tempo do build com mais de 1 GB de payload, a cada tentativa.

---

## Calendário

| Dia | Data | O quê |
|---|---|---|
| seg | 19/10 | À tarde: travar D1 a D5 e mandar os links das telas v2. Se der, a verificação do logoff do RFC-036, à mão |
| ter | 20/10 | Etapa 0: o RFC-037, o esqueleto do `frontend/`, os tokens e a API servindo a interface |
| qua | 21/10 | Etapa 0: o Electron com o launcher, o shell e a tela inicial com dados reais. As medições |
| qui | 22/10 | Etapa 1: o RFC-038, a busca, a grade e os filtros de dispositivo e de data |
| sex | 23/10 | Etapa 1: o detalhe, o Explorer, "perto daqui" e a busca por imagem (backend e interface). As medições |
| sáb e dom | 24 e 25/10 | Folga, ou o que sobrar da Etapa 1 |
| seg | 26/10 | Etapa 2: o RFC-039, a barra lateral, adicionar e renomear dispositivos, e as pastas |
| ter | 27/10 | Etapa 2: os jobs (criar, acompanhar e cancelar) e o teste com um HD externo. As medições |
| qua | 28/10 | Etapa 3: o mapa |
| qui | 29/10 | Etapa 4: o Python embutido e o electron-builder |
| sex | 30/10 | Etapa 4: o teste em conta limpa, o README e o status dos RFCs |
| sáb | 31/10 | Folga e entrega |

---

## Cortes

Se uma etapa atrasar, o corte já está decidido. A ordem é esta:

1. **O mapa vira "perto desta foto".** A Etapa 1 já entrega o filtro por círculo a partir de uma foto. A rota `/images/map` fica sem tela.
2. **O instalador vira um `.zip` portátil.** É `electron-builder --dir` com o Python e o runtime dentro, mais um atalho, sem NSIS.
3. **A busca por upload sai**, e fica *"Parecidas com esta"*, a partir de uma foto indexada.

As etapas 0 a 2 não têm corte. Sem elas, não há produto.

---

## Sobre o RFC-036

O RFC-036 está implementado (`191bde0`). Dele faltam duas coisas: a verificação manual do logoff (§17.3), que é uma sessão encerrada com um job em andamento, e *"uma janela de verdade"*, que é a Etapa 0. O frontend não espera nenhuma das duas. O que a Etapa 0 consome é o contrato do §14, e ele está pronto.

---

## Fora deste roadmap

- O ícone de bandeja e iniciar com o Windows.
- O token por sessão do RFC-036 §14. Conferir o `Host` fecha o DNS rebinding, e não um processo local mal-intencionado.
- A assinatura de código, as atualizações automáticas, as migrations entre versões do app e o `pg_upgrade`.
- Apagar um dispositivo e o rescan (RFC-031 §10), e a paginação de pastas (RFC-031 §8.3).
- As coleções.
- A foto em tamanho original. O detalhe usa a miniatura de 512 px.
- GPU, Linux e macOS.
- Outros idiomas na interface: só PT-BR.
